"""The per-turn event loop.

``_dispatch_events`` reads the session queue, routes each frame (permission requests, KAS
hooks, MCP notifications, steering answers, tool calls and session updates) and runs the
in-band watchdog arm.

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_awaiting_permission``, ``_codex_compaction_pending``,
``_compaction_failed_at``, ``_input_wait_emitted``, ``_last_stop_reason``,
``_mcp_sign_in_completed``, ``_oauth_emitted_servers``, ``_prompt_or_tool_seen``,
``_stale_probe``, ``_tool_dispatched``, ``active_agent``, ``last_compaction_transient``.

Instance fields read: ``_awaited_responses``, ``_cancel_grace_secs``, ``_cancel_ts``,
``_cancelled``, ``_ingress_seq``, ``_kas_compact_turn``, ``_keepalive_parked``,
``_keepalive_ts``, ``_parked_total``, ``_queue``, ``_runtime``, ``_session_id``,
``_skill_reads``, ``_stale_eligible``, ``_steers_proven``, ``_tool_call_diff_path``,
``_tool_call_harness_tool_name``, ``_tool_call_input_redacted``, ``_tool_call_inputs``,
``_tool_call_is_shell``, ``_tool_call_mcp_server``, ``_tool_call_raw_params``,
``_tool_call_tool_name``, ``_turn_done``, ``_watchdog``, ``child_fidelity_aware``,
``last_prompt_stats``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from kiro_crew.acp._dispatch import (
    DRAIN_YIELD_AFTER_S,
    classify_notification,
    error_is_refusal_terminal,
    parse_session_update,
    parse_text_chunk,
    redact_text,
)
from kiro_crew.acp.client import (
    _COMPACTION_FAILED_TURN_BUDGET,
    _is_tool_interrupted_marker,
    _loggable_request_id,
    _raise_acp_error,
    compaction_failure_detail,
    compaction_failure_is_transient,
    format_command_result,
)
from kiro_crew.acp.liveness import (
    EVIDENCE_ESTABLISHED_FLAT,
    EVIDENCE_PLATFORM_LIMITED,
    EVIDENCE_REMOTE_FLAT,
    EVIDENCE_SHELL_CHILD_ABSENT,
    INTERACTIVE_NARROWING_RISKS,
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_WORKING,
)
from kiro_crew.acp.session_handle_watchdog import INTERACTIVE_POLICY_WAIT
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
    EVENT_STRUCTURED_STATUS,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    JSONRPC_METHOD_NOT_FOUND,
    STOP_REASON_CANCELLED,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_END_TURN,
    STOP_REASON_STALE_RECOVER,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

#: The session handle's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_handle")


async def _dispatch_events(
    self: AcpSessionHandle, req_id: int, timeout: float, *, extract_command_result: bool = False
) -> AsyncGenerator[AcpEvent, None]:
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
    # Turn-scoped for the same reason: a flag that leaked into the next turn
    # would settle against an unrelated frame there.
    self._codex_compaction_pending = False
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
    # When the tool branch last read the in-flight tool's subtree as
    # WORKING. The remote_flat narrowing measures its quiet stretch from the
    # later of this and the last own frame, so a remote call that moves bytes
    # between quiet samples is judged on its longest silence, not on the one
    # flat reading that happens to land on a probe tick.
    tool_moved_ts = float("-inf")

    _buffered: list[JsonRpcMessage] = []
    _last_yield = time.monotonic()
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            # Post-compaction-failure budget: automatic compaction reported
            # `failed` and the backend has since gone silent past the budget,
            # so no prompt response or end_turn is coming. End the turn with
            # an explicit stop reason so the caller releases the slot instead
            # of draining to the chat-turn ceiling. Consumer
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
                        self._session_id,
                        self._cancel_grace_secs,
                    )
                    yield AcpEvent(
                        kind=EVENT_COMPLETE,
                        stop_reason=STOP_REASON_STALE_RECOVER,
                        usage=self.last_prompt_stats.to_turn_usage(),
                    )
                    return
                logger.warning(
                    "Cancel unacked after %.1fs on session %s — unblocking caller "
                    "(runtime kept alive for co-tenants)",
                    self._cancel_grace_secs,
                    self._session_id,
                )
                yield AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason="error: cancel unacked",
                    usage=self.last_prompt_stats.to_turn_usage(),
                )
                return

            # Cooperative yield, placed where the previous frame is fully
            # handled and the next one is not yet dequeued: every branch
            # below either runs to the bottom of the loop body or takes a
            # `continue`, so no dequeued frame is held in a local here and
            # both watchdog clocks are already updated. A cancellation
            # landing on this yield therefore drops nothing -- a yield
            # placed right after the dequeue instead would strand the frame
            # in hand, terminal response and death sentinel included. The
            # 5s TimeoutError branch below cannot serve as the yield point
            # either: it fires only on an EMPTY queue, so it is dead during
            # exactly the backlog it would need to guard.
            _now = time.monotonic()
            if _now - _last_yield >= DRAIN_YIELD_AFTER_S:
                await asyncio.sleep(0)
                _last_yield = time.monotonic()

            # A codex steer the adapter answered ``injected`` is proven read
            # before the terminal here, where every frame dequeued so far is
            # fully handled, and only with nothing buffered: the reader
            # enqueues frames and resolves answers inline in stdout order, so
            # with the queue empty a terminal read before the answer would
            # already have ended the turn (see ``_take_injected_steers``).
            # A proven steer is reported consumed only at a clean terminal
            # (``_release_proven_steers``).
            self._steers_proven.extend(
                self._take_injected_steers(can_settle=self._queue.qsize() == 0)
            )

            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=min(remaining, 5.0))
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
                # this session's OWN queue frames and keepalive touches only
                # (progress frames for the session reset last_own_data_ts,
                # and note_keepalive() stamps the touch the folding below
                # reads, so a legitimately-streaming or keepalive-pinging
                # tool keeps the watchdog satisfied, while a co-tenant's
                # ownerless fanned-out frame or keepalive does not defer it).
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
                    #
                    # A keepalive touch for THIS session counts as an own
                    # frame: a blocking MCP tool (wait, spawn_sub_agents)
                    # sends none while it runs and pings this instead. Its
                    # park baseline is the one taken when the touch landed.
                    if self._keepalive_ts > last_own_data_ts:
                        last_own_data_ts = self._keepalive_ts
                        parked_at_own_data = self._keepalive_parked
                    _own_parked = max(0.0, self._parked_total - parked_at_own_data)
                    _tool_idle = max(0.0, (now - last_own_data_ts) - _own_parked)
                    if _tool_idle <= wd.check_after_secs:
                        continue
                    # TOCTOU guard: two complementary signals cover
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
                    _keepalive_before = self._keepalive_ts
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
                        # last_own_data_ts alone, so the deferral cannot
                        # compound into an unbounded one.
                        last_data_ts = time.monotonic()
                        last_own_data_ts = last_data_ts
                        parked_at_own_data = self._parked_total
                        continue
                    if self._keepalive_ts > _keepalive_before:
                        # A keepalive for THIS session landed during the
                        # await: the same activity-in-flight rule as the
                        # frames above, attributed to this session, so it
                        # moves only the tool clock, to the touch's stamp.
                        last_own_data_ts = self._keepalive_ts
                        parked_at_own_data = self._keepalive_parked
                        continue
                    if verdict == VERDICT_WORKING:
                        # Stamped after the consult returns: the probe
                        # observed the tool at the end of its await, so
                        # the pre-consult clock would shorten the window.
                        tool_moved_ts = time.monotonic()
                        if not self._working_tool_bound_reached(evidence, _tool_idle):
                            self._log_working_deferral(_tool_idle, evidence, timeout)
                            continue
                        # Opaque-MCP WORKING past the hard cap: fall
                        # through to the same session-scoped recovery
                        # an UNKNOWN past the cap gets below.
                    # UNKNOWN acts at the suspect window. The suspect
                    # default (90 min) is BUILD-scale forbearance — an LLM-shaped
                    # stall (flat subtree whose only live evidence is an
                    # established backend socket: a model turn riding inside
                    # a tool, e.g. kiro-cli use_subagent) narrows to the
                    # model-silent budget, because its longest legitimate
                    # silent gap is minutes, not hours. Every narrowing below
                    # is keyed STRICTLY on an oracle evidence TAG:
                    # established_flat; shell_child_absent, a shell command
                    # with no process to its name; platform_limited, only
                    # when the tool layer classified the command
                    # prompt-shaped; remote_flat, only while
                    # remote_flat_probe_secs is on. Evidence matching none
                    # of them (a quiet build's unmatched-but-live tree, for
                    # one) keeps the full window.
                    # Hard cap: watchdog_tool_stall_hard_cap_secs is
                    # the absolute ceiling for UNKNOWN forbearance. Apply
                    # min(suspect_window, hard_cap) so the configured cap
                    # always bounds the effective window. WORKING deferred
                    # above, except an opaque-MCP reading past the hard
                    # cap; DEAD/STUCK_INPUT act immediately regardless of
                    # the window.
                    # The action below is the non-lethal tool-stall
                    # recovery.
                    _suspect = wd.tool_stall_suspect_secs
                    _full_suspect = min(_suspect, wd.tool_stall_hard_cap_secs)
                    # Idle measure the chosen window is compared against. Only
                    # the remote_flat narrowing swaps it for a stricter one.
                    _window_idle = _tool_idle
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
                    elif (
                        evidence.startswith(EVIDENCE_PLATFORM_LIMITED)
                        and self._inflight_interactive is not None
                        and self._inflight_interactive.risk in INTERACTIVE_NARROWING_RISKS
                    ):
                        # The platform cannot see a stdin block, and the
                        # dispatched command is exactly the shape that
                        # evidence would have caught (an editor, a REPL,
                        # a confirmation, a credential prompt). Narrow to
                        # the ordinary silence window: a prompt-shaped
                        # command that has gone flat for 10 minutes is
                        # waiting for input, not building. A plain
                        # ``platform_limited`` (no interactive risk) keeps
                        # the build-scale window — "alive" is still
                        # bounded, just by the standard budget.
                        _narrowed = True
                        _suspect = min(wd.stale_window_secs, _suspect)
                    elif (
                        evidence.startswith(EVIDENCE_REMOTE_FLAT) and wd.remote_flat_probe_secs > 0
                    ):
                        # An MCP tool blocked on its own remote call: the
                        # tree is flat and the tool's own server holds an
                        # established TCP connection. With no client timeout
                        # a peer that never answers holds the call until the
                        # build-scale window, which is the hang users see as
                        # a tool that runs forever. Narrowed to the
                        # remote-call budget, measured as the stretch with
                        # neither an own frame NOR any WORKING reading of the
                        # tree, so a stream that moves now and then keeps
                        # the full window. 0 turns the narrowing off.
                        _narrowed = True
                        _suspect = min(wd.remote_flat_probe_secs, _suspect)
                        _window_idle = max(0.0, min(_tool_idle, now - tool_moved_ts))
                    _suspect = min(_suspect, wd.tool_stall_hard_cap_secs)
                    _acting = (
                        verdict in (VERDICT_DEAD, VERDICT_STUCK_INPUT)
                        or _window_idle > _suspect
                        or _tool_idle > _full_suspect
                    )
                    if not _acting:
                        continue  # UNKNOWN, within budget — keep waiting
                    # Post-stall classification (RFC §14.6): STUCK_INPUT, or
                    # a platform-limited no-progress verdict on a
                    # prompt-shaped command, is a WAIT FOR INPUT (W4), not an
                    # opaque stall. The typed status is yielded BEFORE any
                    # action so a scheduler can release the lane slot on it;
                    # nothing here answers the prompt, and the status's
                    # ``safe_retry`` says whether a non-interactive re-run
                    # could repeat a side effect (it is False once the call
                    # streamed output).
                    _input_wait = self._input_wait_status(verdict, evidence)
                    if _input_wait is not None and not self._input_wait_emitted:
                        self._input_wait_emitted = True
                        yield AcpEvent(
                            kind=EVENT_STRUCTURED_STATUS,
                            tool_call_id=_input_wait.tool_call_id,
                            status=_input_wait,
                        )
                    if (
                        _input_wait is not None
                        and wd.interactive_command_policy == INTERACTIVE_POLICY_WAIT
                    ):
                        # ``wait``: keep the turn open for real input. The
                        # blocked process is kept (residency stays charged),
                        # the slot is the scheduler's to release on the
                        # status above, and the turn's own ceiling — the
                        # task's deadline — is the bound. Never a cancel,
                        # never an auto-answer.
                        continue
                    if verdict == VERDICT_WORKING:
                        _cancel_window = "working_cap"
                    elif _narrowed:
                        _cancel_window = "narrowed"
                    else:
                        _cancel_window = "standard"
                    self._emit_watchdog_metric(
                        "cancel",
                        verdict,
                        evidence,
                        _tool_idle,
                        window=_cancel_window,
                    )
                    async for ev in self._end_stalled_tool(
                        verdict, evidence, _tool_idle, status=_input_wait
                    ):
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
                        window = wd.model_silent_probe_secs if _flat_wait else wd.stale_window_secs
                        if _stale_idle <= min(window, wd.tool_stall_hard_cap_secs):
                            continue
                    # DEAD, or UNKNOWN past its window: probe via session/cancel.
                    # The probe is NON-LETHAL either way — a live turn's cancel
                    # ack is reclassified to STOP_REASON_STALE_RECOVER in the
                    # turn-complete branch (auto-recovery, never "cancelled by
                    # user"), and an unacked cancel confirms the wedge via the
                    # unresponsive-cancel branch at the loop top.
                    # ``window`` = "extended" when the established_flat
                    # model-wait probe window (model_silent_probe_secs, 1800s)
                    # governed the decision instead of the ordinary stale
                    # window (stale_window_secs, 600s). The established_flat
                    # case is an EXTENSION for model-wait (silence of a
                    # non-streamed think), not a narrowing as on the tool
                    # branch — emitting "extended" lets dashboards distinguish
                    # the two cases correctly.
                    self._emit_watchdog_metric(
                        "probe",
                        verdict,
                        evidence,
                        _stale_idle,
                        window="extended" if _flat_wait else "standard",
                    )
                    logger.warning(
                        "Stale turn on session %s (idle %.0fs, verdict=%s: %s) — "
                        "probing via session/cancel",
                        self._session_id,
                        _stale_idle,
                        verdict,
                        evidence,
                    )
                    try:
                        await asyncio.wait_for(self.cancel(_stale_probe=True), timeout=5.0)
                    except Exception:
                        logger.debug(
                            "stale-probe session/cancel failed for %s",
                            self._session_id,
                            exc_info=True,
                        )
                continue

            if msg is None:
                # Runtime process died — sentinel
                raise self._died("Runtime process died during prompt")

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
                    if error_is_refusal_terminal(msg.error, self.last_prompt_stats.refusal):
                        # A content-filter refusal whose terminal is the
                        # bare ``-32603 Internal error``: the reason already
                        # arrived on metadata, so this is the refusal's own
                        # terminal, not a backend fault. Raising would lose
                        # the reason to the unknown-shape formatter and hand
                        # a deterministic decline to the retry ladder.
                        reason, _refusal = self.last_prompt_stats.terminal_refusal("")
                        self._last_stop_reason = reason
                        self._tool_dispatched = False
                        self._turn_done.set()
                        yield AcpEvent(
                            kind=EVENT_COMPLETE,
                            stop_reason=reason,
                            refusal=_refusal,
                            usage=self.last_prompt_stats.to_turn_usage(),
                        )
                        return
                    # Same as _wait_for_response: route through the shared
                    # raise helper so a mid-turn failure surfaces actionable
                    # prose instead of the raw JSON-RPC dict, keeps its
                    # transient verdict for the retry ladder, and raises
                    # AcpPromptBusy when the backend reports a concurrent
                    # in-flight prompt. Advertised ids feed the entitlement
                    # discriminator (see _wait_for_response).
                    _raise_acp_error(
                        msg.error,
                        self._advertised_model_ids(),
                        backend=self._runtime.acp_backend,
                    )
                result = msg.result or {}
                reason = ""
                if isinstance(result, dict):
                    reason = result.get("stopReason", "") or ""
                if getattr(self, "_kas_compact_turn", False):
                    # ``_kiro/session/compact`` answers ``{success}`` with no
                    # stopReason; the answer IS the end of this turn.
                    _kas_status = self._settle_kas_compaction(result)
                    if _kas_status is not None:
                        yield _kas_status
                    reason = STOP_REASON_END_TURN
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
                        self._session_id,
                        STOP_REASON_STALE_RECOVER,
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
                                agent_info.get("name", "") if isinstance(agent_info, dict) else ""
                            )
                            if name:
                                name = self._crew_agent_name(name)
                                self.active_agent = name
                                yield AcpEvent(kind=EVENT_AGENT_SWITCHED, text=name)
                reason, _refusal = self.last_prompt_stats.terminal_refusal(reason)
                self._last_stop_reason = reason
                self._tool_dispatched = False
                for _injected in self._release_proven_steers(reason, _refusal):
                    yield AcpEvent(kind=EVENT_STEER_CONSUMED, text=_injected)
                self._turn_done.set()
                yield AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=reason,
                    refusal=_refusal,
                    usage=self.last_prompt_stats.to_turn_usage(),
                )
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
                        "Dropping stray response frame id=%s (no waiter)",
                        _loggable_request_id(msg.id),
                    )
                continue

            # The backend's hooks requests, answered here rather
            # than through the shared classifier: that classifier is also read
            # by the single-session client, which serves no hooks surface, and
            # naming an action there that only this loop handles would leave
            # the request unanswered on that path instead of refused.
            #
            # Gated on the capability set, not on the method name alone. This
            # loop is shared by every backend the runtime demuxes, and only one
            # of them defines this channel -- the answers carry
            # operator-authored hook commands, so a backend that never asked for
            # the surface is answered -32601 like any other method it does not
            # serve.
            if self._is_kas_hooks_request(msg):
                await self._answer_kas_hooks_request(msg)
                continue

            self._note_mcp_sign_in_status(msg, offer=True)
            # A server this session was signing in to has connected. The
            # status snapshot is classified "skip", so its completion is
            # yielded here, as soon as the snapshot is read; a completion
            # read by a drain is yielded with the turn's first frame. Popped
            # before the yield so a consumer that closes the turn mid-yield
            # does not see the same completion twice.
            while self._mcp_sign_in_completed:
                _done = self._mcp_sign_in_completed.pop()
                # Mirrors the mcp_server_initialized branch: a later
                # token-expiry retry may surface a new banner.
                self._oauth_emitted_servers.discard(_done)
                yield AcpEvent(
                    kind=EVENT_MCP_SERVER_INITIALIZED,
                    server_name=_done,
                    runtime_global=False,
                )

            # Dispatch by method
            action = self._classify(msg)

            if action == "permission":
                _perm_event = self._build_permission_event(msg)
                if _perm_event is None:
                    if msg.id is not None:
                        await self._runtime.send_error(msg.id, -32600, "invalid request id")
                    continue
                # Before the fidelity gate: a tool the spec switched off is
                # refused whether or not this consumer opted into the child
                # contract, and naming that reason in the audit is more use
                # than naming the fidelity one for the same rejected call.
                if await self._deny_spec_disabled_tool(_perm_event):
                    continue
                if await self._refuse_unidentifiable_mcp_approval(msg, _perm_event):
                    continue
                if await self._refuse_unreadable_mcp_identity(_perm_event):
                    continue
                if await self._refuse_unretained_provenance(msg, _perm_event):
                    continue
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
                        _loggable_request_id(_perm_event.request_id),
                        _loggable_request_id(_perm_event.sub_session_id),
                    )
                    self._audit_handle_reject(
                        _perm_event.request_id,
                        _perm_event.title or "",
                        "child_low_fidelity_unaware_consumer",
                        sub_session_id=_perm_event.sub_session_id or "",
                    )
                    await self.reject_tool(_perm_event.request_id)
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
            elif action == "elicitation":
                # An MCP server's human-in-the-loop ask. The handle answers it
                # itself (decline) unless this consumer can put it to a person;
                # the returned event is the ask that consumer must answer. No
                # trust tier applies: it is the server's requirement, not ours.
                _elicit_event = await self._handle_elicitation_request(msg)
                if _elicit_event is None:
                    continue
                # Same park marking as a permission: the consumer waits on a
                # human here too, and an observer must not read that as a stall.
                self._awaiting_permission = True
                yield _elicit_event
            elif action == "server_request_unknown":
                await self._runtime.send_error(msg.id, JSONRPC_METHOD_NOT_FOUND, "Method not found")
            elif action == "update":
                for ev in self._handle_update(msg):
                    # Before the yield, so the observation is recorded even for a
                    # consumer that stops pulling: the call already ran, and this
                    # is the only in-band notice that it did.
                    self._tripwire_spec_disabled_tool(ev, msg)
                    # Skill-read credit, awaited per frame so a call is noted
                    # before its completion frame can arrive. The initial
                    # tool_call and its refinement are both noted (the
                    # arguments may ride either); the tracker dedupes by id.
                    # Child-routed frames surface as subagent activity, not
                    # these kinds, so a native child's reads are not credited.
                    if ev.kind in (EVENT_TOOL_CALL, EVENT_TOOL_CALL_UPDATE):
                        await self._skill_reads.note(ev)
                    elif ev.kind == EVENT_TOOL_RESULT:
                        self._skill_reads.credit(ev)
                    # After the tripwire read the row: a terminal frame settles
                    # the call, and its provenance is released so the caches
                    # hold calls in flight, not the turn's history.
                    _upd = (msg.params or {}).get("update")
                    self._release_settled_tool_call(
                        _upd, str((msg.params or {}).get("sessionId") or self._session_id)
                    )
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
                        yield AcpEvent(
                            kind=EVENT_COMPLETE, usage=self.last_prompt_stats.to_turn_usage()
                        )
                        return
            elif action == "steer":
                # Mid-turn steer lifecycle echo from kiro-cli (_session/steer).
                # queued carries the pending snapshot; consumed carries injected
                # text. Never trust backend-echoed steer text: redact before it
                # can reach any surface. Mirrors AcpClient._dispatch_events.
                params = msg.params or {}
                _steer_sid = str(params.get("sessionId") or "")
                if _steer_sid and _steer_sid != self._session_id:
                    # A steer echo naming ANOTHER session — an announced
                    # child's frame routed to this single-owner queue
                    # (either session/update spelling). Only a frame this
                    # session OWNS may settle this session's steer
                    # ledger: surfacing a child's steering_consumed as a
                    # parent EVENT_STEER_CONSUMED could settle a pending
                    # user steer or policy-refusal continuation the
                    # parent backend never consumed. Same trust boundary
                    # the compaction branch draws with owns_frame.
                    continue
                _upd = params.get("update")
                _upd = _upd if isinstance(_upd, dict) else {}
                _disc = str(_upd.get("sessionUpdate") or "")
                _text = redact_text(str(_upd.get("content") or _upd.get("message") or ""))
                # An echo that named no session and was fanned out to
                # co-tenants is not this session's own (``runtime_global``).
                _ownerless = msg.fanout_no_owner
                if _disc in ("steering_queued", "AgentExecutionUserMessageQueued"):
                    yield AcpEvent(kind=EVENT_STEER_QUEUED, text=_text, runtime_global=_ownerless)
                elif _disc in ("steering_consumed", "AgentExecutionSteeringInjected"):
                    yield AcpEvent(kind=EVENT_STEER_CONSUMED, text=_text, runtime_global=_ownerless)
                elif _disc == "steering_cleared":
                    yield AcpEvent(kind=EVENT_STEER_CLEARED, runtime_global=_ownerless)
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
                # hang the budget exists to close.  Same trust boundary
                # the budget's own clock already draws (last_own_data_ts) and
                # the subagent roster already draws (runtime_global=).  A lone
                # session's frame is left unmarked and genuinely is its own,
                # so a single-session run is unaffected.  The event still
                # surfaces either way, carrying that provenance
                # (``runtime_global``) so a consumer measuring this session's
                # own activity can tell; only the mutations are gated.
                owns_frame = not msg.fanout_no_owner
                if status_type == "completed" and owns_frame:
                    # The pre-compaction counts (and their authoritative
                    # context_tokens_from_usage flag) do not describe
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
                        self.last_compaction_transient = compaction_failure_is_transient(params)
                    summary = compaction_failure_detail(params)
                yield AcpEvent(
                    kind=EVENT_COMPACTION_STATUS,
                    text=status_type,
                    title=summary,
                    runtime_global=not owns_frame,
                )
            elif action == "clear":
                # Same provenance as the compaction notice: one that named
                # no session and was fanned out to co-tenants is not this
                # session's own.
                yield AcpEvent(kind=EVENT_CLEAR_STATUS, runtime_global=msg.fanout_no_owner)
            elif action == "agent_switched":
                saw_agent_switch = True
                params = msg.params or {}
                name = params.get("agentName", "")
                name = self._crew_agent_name(name) if isinstance(name, str) else ""
                self.active_agent = name
                yield AcpEvent(
                    kind=EVENT_AGENT_SWITCHED,
                    text=name,
                    runtime_global=msg.fanout_no_owner,
                )
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
                    if not msg.fanout_no_owner:
                        # Native-subtask residency seam (RFC §14.8): a
                        # roster this handle provably owns names ITS
                        # children. Fanned-out rosters name no owner and
                        # must not be counted on every co-tenant.
                        self._note_native_roster(subs)
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
                if ssid and ssid != self._session_id:
                    # Native-subtask residency seam (RFC §14.8): same
                    # count as _handle_update's plain-spelling route.
                    self._note_native_child(ssid)
                    # A backend-internal child's update under the
                    # extension method `_kiro.dev/session/update`. BOTH
                    # session-update spellings are live carriers, not a
                    # version succession: kiro-cli 2.21.x uses the
                    # extension method for the child stream regardless
                    # of the plain/extension ordering the steer comment
                    # in _dispatch.classify_notification describes. Run
                    # the payload through the shared parser for its
                    # cache SIDE EFFECTS ONLY — the origin-scoped
                    # per-toolCallId writes (command bytes, raw params,
                    # shell classification, and the `_meta.kiro`
                    # server/tool identity) that a later child
                    # permission request's trust split reads. Without
                    # this parse the caches stay empty for the extension
                    # spelling, a child MCP call cannot verify its
                    # identity, and every auto-approve path falls to the
                    # interactive UNVERIFIED card. Identity still comes
                    # ONLY from a frame this client parsed: the runtime
                    # routes the method solely for an announced child on
                    # a single-owner runtime. Same
                    # cache-side-effects-only shape as the KAS child
                    # nested-tool path in _handle_update.
                    #
                    # The activity events stay the hand-rolled yields
                    # below, and they INTENTIONALLY differ from
                    # _handle_update's child re-tag path (the plain
                    # spelling's route): this branch emits activity for
                    # any update carrying a toolCallId — including
                    # discriminant-less frames and tool_call_update
                    # refinements the parser suppresses — a display
                    # shape pinned by the pre-existing
                    # test_dispatch_subagent_activity_* tests. The
                    # security caches are the shared, parser-derived
                    # part; the coarse crew-monitor display is not.
                    parse_session_update(
                        upd,
                        tool_input_cache=self._tool_call_inputs,
                        tool_input_redacted_cache=self._tool_call_input_redacted,
                        shell_cache=self._tool_call_is_shell,
                        raw_params_cache=self._tool_call_raw_params,
                        diff_path_cache=self._tool_call_diff_path,
                        tool_kind_cache=self._tool_kind_cache_for_wire(),
                        mcp_server_name_cache=self._tool_call_mcp_server,
                        tool_name_cache=self._tool_call_tool_name,
                        harness_tool_name_cache=self._tool_call_harness_tool_name,
                        identity_unreadable_cache=self._identity_unreadable_cache_for_wire(),
                        cache_scope=ssid,
                        cache_overflow=self._tool_call_cache_overflow_for_wire(),
                    )
                    self._release_settled_tool_call(upd, ssid)
                tcid = str(upd.get("toolCallId") or "")
                # Single-source the text-shape read via the shared parser so the
                # sub-agent text path matches the main one (content.text + flat).
                _su_text_val, _su_thinking = parse_text_chunk(upd)
                su_text = _su_text_val or ""
                su_kind = str(upd.get("sessionUpdate") or "")
                # A frame naming THIS session is this turn's own stream, not a
                # child's, so it yields no sub-agent activity. kiro-cli sends
                # the extension spelling for the parent's own tool-call chunk
                # as well as for a child's update, and the two are told apart
                # only by the sessionId -- the same discriminant the cache
                # scoping above uses. Without this the crew monitor shows a
                # sub-agent for every tool call of an ordinary turn, whose id
                # is the session the user is already looking at.
                own_session = bool(ssid) and ssid == self._session_id
                if own_session:
                    continue
                if ssid and tcid:
                    # A child's tool call is this session's side effect for
                    # replay purposes, whichever spelling carried it: close
                    # the registration-throttle window here exactly as the
                    # plain-spelling route does.
                    self._prompt_or_tool_seen = True
                    yield AcpEvent(
                        kind=EVENT_SUBAGENT_ACTIVITY,
                        sub_session_id=ssid,
                        tool_call_id=tcid,
                        title=redact_text(str(upd.get("title") or "")),
                    )
                elif ssid and su_text and su_kind == "agent_message_chunk" and not _su_thinking:
                    # A child's streamed text closes the window too: work
                    # was observed, and a child whose tool frame was lost or
                    # differently spelled must not read as "did nothing".
                    self._prompt_or_tool_seen = True
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
        yield AcpEvent(
            kind=EVENT_COMPLETE,
            stop_reason="timeout",
            usage=self.last_prompt_stats.to_turn_usage(),
        )
    finally:
        for _m in _buffered:
            self._queue.put_nowait(_m)


def _classify(self: AcpSessionHandle, msg: JsonRpcMessage) -> str:
    """Classify a notification message into an action string."""
    return classify_notification(msg)
