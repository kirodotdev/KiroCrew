"""One turn end to end.

``prompt`` and ``stream_command``, the per-turn reset and pre-turn drain in ``_run_turn``,
the abandoned-turn close and the turn-done wait.

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_abandoned_prompt_id``, ``_awaiting_permission``, ``_between_turn_credits``,
``_cancel_ts``, ``_cancelled``, ``_compaction_terminal_seen``, ``_input_wait_emitted``,
``_kas_compact_turn``, ``_kas_subagent_roster``, ``_last_stop_reason``,
``_native_child_overflow``, ``_native_child_sids``, ``_native_child_tool_call_ids``,
``_parked_since``, ``_parked_total``, ``_pending_reject_notices``,
``_permission_gate_events``, ``_permission_options``, ``_prompt_starts``,
``_prompt_written``, ``_skill_reads``, ``_stale_eligible``, ``_stale_probe``,
``_status_rejected``, ``_steers_accepted``, ``_steers_proven``,
``_tool_call_cache_overflow``, ``_tool_call_diff_path``,
``_tool_call_harness_tool_name``, ``_tool_call_identity_unreadable``,
``_tool_call_input_redacted``, ``_tool_call_inputs``, ``_tool_call_is_shell``,
``_tool_call_kind``, ``_tool_call_mcp_server``, ``_tool_call_raw_params``,
``_tool_call_tool_name``, ``_tool_dispatched``, ``_tool_output_seen``,
``_tool_tracker``, ``_turn_done``, ``_turn_steering_denied``, ``_working_logged_ts``,
``last_compaction_transient``, ``last_infra_error``, ``last_prompt_stats``.

Instance fields read: ``_abandoned_steering``, ``_awaited_responses``,
``_mode_model_unjudged``, ``_queue``, ``_runtime``, ``_session_id``,
``_steering_cancel_tasks``.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from kiro_crew.acp import kas_profile
from kiro_crew.acp._dispatch import (
    is_unattributed_error_candidate,
    prompt_is_adapter_command,
    redact_text,
    reject_option_id,
    unattributed_terminal_error,
)
from kiro_crew.acp.client import (
    AcpError,
    _effective_prompt_timeout_async,
    _loggable_request_id,
    _raise_acp_error,
    parse_slash_command,
)
from kiro_crew.acp.elicitation import (
    ELICITATION_CANCEL,
    elicitation_response,
    is_elicitation_request,
)
from kiro_crew.acp.prompt_blocks import build_prompt_blocks, summarize_prompt_structure
from kiro_crew.acp.session_handle_lifecycle import _CANCEL_GRACE_SECS
from kiro_crew.acp.session_handle_runtime_contract import AcpRuntimeError
from kiro_crew.acp.session_handle_watchdog import _WORKING_NEVER_LOGGED
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR,
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_STEER_CONSUMED,
    EVENT_SUBAGENT_ACTIVITY,
    METHOD_CANCEL,
    METHOD_COMMANDS_EXECUTE,
    METHOD_KAS_SESSION_COMPACT,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_PROMPT,
    METHOD_REQUEST_PERMISSION,
    STOP_REASON_CANCELLED,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_CONTENT_FILTERED_WIRE,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TOOL_STALL,
    AcpEvent,
    JsonRpcMessage,
)

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

#: The session handle's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_handle")


# The stopReason values the pre-turn drain may NAME in its warning: the closed
# protocol values (``types.STOP_REASON_*``) only. A discarded terminal whose
# stopReason is any other wire string still COUNTS, but its value is logged as
# a placeholder — an unrecognized string could carry anything, and frame
# content never belongs in a log (the closed-values discipline of
# ``chat_runner``'s empty-turn line).
_DRAIN_CLOSED_STOP_REASONS = frozenset(
    (
        STOP_REASON_CANCELLED,
        STOP_REASON_COMPACTION_FAILED,
        STOP_REASON_CONTENT_FILTERED_WIRE,
        STOP_REASON_END_TURN,
        STOP_REASON_REFUSAL,
        STOP_REASON_STALE_RECOVER,
        STOP_REASON_TOOL_STALL,
    )
)


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


async def wait_turn_done(self: AcpSessionHandle, timeout: float = 30.0) -> bool:
    """Wait for the current turn to complete. Returns True if done, False on timeout."""
    try:
        await asyncio.wait_for(self._turn_done.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        return False


# ── Prompt ──


async def prompt(
    self: AcpSessionHandle, message: str, timeout: float | None = None, *, allow_image: bool = True
) -> AsyncIterator[AcpEvent]:
    """Send session/prompt and yield AcpEvent objects until the turn completes.

    Dispatches events from the per-session queue with the same logic as
    AcpClient._dispatch_events. Detects turn boundaries via the JSON-RPC
    response matching the prompt's request_id.

    ``timeout=None`` (every dashboard turn) resolves from
    ``agent.chat_turn_timeout_secs`` so the transport wait follows a raised
    turn ceiling instead of cutting the turn at the 2h default underneath it.

    ``allow_image=False`` sends the message as text only: no path in it is
    read or inlined, whatever the agent advertises. For a prompt that is
    text ABOUT a session, where a path is quoted history, not an attachment.
    """

    async def _build() -> tuple[str, dict[str, Any]]:
        if self._kas_compact_request(message):
            # The engine's own compaction verb, not prompt text: KAS reads a
            # ``/compact`` prompt as a request to write a summary, and the
            # context does not shrink. The verb takes no instructions, so any
            # text after ``/compact`` is not sent.
            self._kas_compact_turn = True
            return METHOD_KAS_SESSION_COMPACT, {"sessionId": self._session_id}
        # Offloaded: the builder stats and reads image files (up to
        # MAX_IMAGE_BYTES each) and base64-encodes them. Inline, that
        # blocking I/O runs on the gateway loop and pauses every other
        # session's streaming for the duration.
        prompt_blocks = await asyncio.to_thread(
            build_prompt_blocks,
            message,
            allow_image=allow_image,
            agent_accepts_image=self._runtime.supports_image_prompt,
        )
        # Content-free outbound STRUCTURE diagnostics: one
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
        params: dict[str, Any] = {
            "sessionId": self._session_id,
            # An image reaches the model ONLY as an image block. Sending a
            # local image path as a single text block would ship a
            # filesystem path as prose (Slack, dashboard) and the model
            # would never see the picture. Gated on the agent's advertised
            # capability; when it is absent the path stays in the text as a
            # tool-openable reference rather than being dropped.
            "prompt": prompt_blocks,
        }
        if getattr(self._runtime, "acp_backend", "") == ACP_BACKEND_KAS:
            # The engine's per-turn context breakdown and time to first token
            # (see ``kas_profile``). Positive KAS gate: no other harness is sent a
            # ``_meta`` it never defined.
            params["_meta"] = copy.deepcopy(kas_profile.PROMPT_META)
        return METHOD_PROMPT, params

    # Explicit aclose in a finally: abandoning THIS wrapper (gen.aclose()
    # at any yield) must finalize the inner turn generator NOW — its
    # finally is what unmarks the turn and re-sets _turn_done. Left to the
    # event loop's async-generator GC hook, the handle would read as
    # turn-active until some later collection pass.
    if self._kas_compact_request(message) and not self._kas_compact_advertised():
        # An engine that does not list the verb would answer it with a
        # JSON-RPC error, and the prompt does not compact on KAS. Report a
        # failed compaction without sending anything.
        self.last_compaction_transient = False
        yield AcpEvent(
            kind=EVENT_COMPACTION_STATUS,
            text="failed",
            title=(
                "this KAS build offers no compaction command; it still "
                "summarizes on its own as the context fills"
            ),
        )
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
        return
    turn = self._run_turn(_build, timeout)
    try:
        async for event in turn:
            yield event
    finally:
        await turn.aclose()


async def stream_command(
    self: AcpSessionHandle, command: str, timeout: float | None = None
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
    - a non-kiro backend — ``_kiro.dev/commands/execute`` is
      kiro-cli-specific, so every other harness takes session/prompt instead
      of an unimplemented method. The dashboard chat path answers KAS harness
      commands, and ``/clear`` on every ``ACP_BACKENDS_LOCAL_CLEAR`` member,
      itself before reaching here (``_answer_slash_without_channel``).

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
        self._runtime.acp_backend == ACP_BACKEND_KIRO and cmd_name not in _PROMPT_TRANSPORT_COMMANDS
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
    self: AcpSessionHandle,
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
    # An abandoned answer leaves ``_abandoned_steering`` in its own
    # done-callback, before the cancel it scheduled is written, so a scheduled
    # cancel alone must still hold the prompt back until it has gone out.
    if getattr(self, "_abandoned_steering", None) or getattr(self, "_steering_cancel_tasks", None):
        await self._settle_abandoned_steering()
    if self._abandoned_prompt_id is not None:
        await self._close_abandoned_turn()
    if getattr(self, "_mode_model_unjudged", False):
        # Before the turn is marked active: a set_model the judgement sends
        # awaits its own answer on this queue.
        await self._served_default_after_set_mode()
    # An elicitation the last turn left unanswered holds its tool call open on
    # the engine; it is answered ``cancel`` rather than merely forgotten. Before
    # the turn is marked active: the answer is a write that can be cancelled,
    # and a cancellation after ``_turn_done.clear()`` would leave the handle
    # turn-active with nothing left to set it again.
    await self.cancel_pending_elicitations()

    self._cancelled = False
    self._cancel_ts = 0.0
    self._turn_done.clear()
    self._prompt_starts += 1
    # Reset the stored stop_reason: only a real `complete` response sets it,
    # and the synthetic-terminal paths (cancel-unacked / stale / tool-stall /
    # timeout) call _turn_done.set() WITHOUT updating it. Without this reset,
    # wait_turn_done() would return the PREVIOUS turn's reason (e.g. a stale
    # "end_turn" making a timed-out cancel look acked, or "" → a spurious
    # hard kill of the shared runtime). Mirrors AcpClient.
    self._last_stop_reason = ""
    # Set by prompt()'s builder when this turn is a KAS compaction request;
    # the terminal flag records that the engine already reported its outcome.
    self._kas_compact_turn = False
    self._compaction_terminal_seen = False
    self._stale_eligible = False
    # Set when a genuine stale turn is probed via session/cancel; read by the
    # unresponsive-cancel branch to distinguish a confirmed wedge (signal
    # auto-recovery) from an ordinary unacked cancel (unblock caller).
    self._stale_probe = False
    # A new turn starts with no infrastructure verdict carried over.
    self.last_infra_error = None
    self._tool_dispatched = False
    self._report_evicted_attributions()
    self._tool_tracker.clear()
    self._tool_output_seen.clear()
    self._status_rejected.clear()
    self._input_wait_emitted = False
    self._native_child_sids.clear()
    self._native_child_overflow = 0
    # Park state is per-turn: carrying it across would charge the previous
    # turn's consumer time to this one, and a permission left unanswered when
    # the last turn died would mask this turn's stalls forever.
    self._parked_total = 0.0
    self._parked_since = None
    self._awaiting_permission = False
    self._turn_steering_denied = False
    self._steers_proven = []
    self._steers_accepted = []
    self._prompt_written = False
    self._retire_liveness_state()
    self._working_logged_ts = _WORKING_NEVER_LOGGED
    self._tool_call_inputs.clear()
    self._tool_call_input_redacted.clear()
    self._tool_call_is_shell.clear()
    self._tool_call_raw_params.clear()
    self._skill_reads.clear()
    self._tool_call_diff_path.clear()
    self._tool_call_kind.clear()
    self._tool_call_cache_overflow.clear()
    self._tool_call_mcp_server.clear()
    self._tool_call_tool_name.clear()
    self._tool_call_harness_tool_name.clear()
    self._tool_call_identity_unreadable.clear()
    self._native_child_tool_call_ids.clear()
    self._permission_options.clear()
    self._permission_gate_events.clear()
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
    # process teardown — a two-hour crew stall. A request stranded
    # from an abandoned turn (or routed here for a backend child between
    # turns) gets the fail-closed reject; the live turn's requests are
    # handled by the dispatch loop as before.
    # A DROPPED frame is invisible to every layer above: the abandoned turn's
    # output vanishes here with nothing to show it existed, and a turn that
    # loses its terminal this way reaches the dashboard as an empty response
    # with no attributable cause. Count them and say how many, ONCE. Never
    # what they were: a frame carries model text, tool arguments and tool
    # results, and none of that belongs in a log — nor its size, which leaks
    # response length. The one exception is a discarded terminal's
    # stopReason, logged only as a closed protocol value (see
    # _DRAIN_CLOSED_STOP_REASONS). The count is bounded by the queue, and
    # the log line is one per turn regardless of how many frames drained.
    #
    # ONE class of leftover frame is NOT the abandoned turn's to destroy.
    # The abandoned turn's own frames are owed to nobody: this method
    # refuses to start while _turn_done is clear and sets it in its own
    # finally, so that turn's generator has already exited by the time the
    # drain runs — dropping them loses nothing a consumer was still waiting
    # for. A command/config call is different. send_command / compact /
    # set_config_option never touch _turn_done, so their _wait_for_response
    # can be IN FLIGHT when the next turn starts, and for a oneshot the
    # response IS the terminal. Destroying it strands that caller until its
    # own timeout (60s for send_command, which then reports "" for a call
    # the backend answered). _awaited_responses names exactly the req_ids
    # still being waited on, which is the discriminator the dispatch loop
    # already routes responses on; retain those and let everything else
    # drain. Re-injected AFTER the loop, never inside it: putting a frame
    # back into the queue being drained would loop forever (the same reason
    # _wait_for_response re-injects from its finally).
    _stale_dropped = 0
    _stale_terminals = 0
    _stale_reasons: list[str] = []
    _stale_owed: list[JsonRpcMessage] = []
    _sign_in_holds = getattr(self._runtime, "mcp_sign_in_holds", None)
    while True:
        try:
            stale = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if stale is not None:
            # A between-turns MCP status snapshot still tells this session
            # which servers need a sign-in: a server that connected while
            # no turn was reading leaves the set here, so the turn-start
            # offer below never resets a connected server (a reset with
            # startOAuth invalidates its credentials). Read only, no offer:
            # the offer is made once, after the drain.
            self._note_mcp_sign_in_status(stale, offer=False)
        if (
            stale is not None
            and stale.method is None
            and stale.id is not None
            and stale.id in self._awaited_responses
        ):
            # A live waiter is inside _wait_for_response for this id. Hand
            # it back instead of counting it: it is not a lost frame.
            _stale_owed.append(stale)
            continue
        if (
            stale is not None
            and stale.method is not None
            and stale.is_method(METHOD_MCP_OAUTH_REQUEST)
            and self._owns_mcp_frame(stale)
            and _sign_in_holds is not None
            and _sign_in_holds(self._session_id, stale.params.get("serverName", ""))
        ):
            # The consent URL of a sign-in this session started, delivered
            # while no turn was reading. It is the one link the user can
            # complete (the runtime's sign-in slot stays held for it), so
            # it is kept for the dispatch loop to yield as this turn's
            # EVENT_MCP_OAUTH_REQUEST rather than destroyed with the
            # abandoned turn's frames.
            _stale_owed.append(stale)
            continue
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
            # Hand back everything retained so far BEFORE the await below,
            # which is the ONE await in this loop. reject_tool writes to the
            # child's stdin and that write is not bounded short: a backend
            # that already delivered a command response but has stopped
            # reading its own stdin applies backpressure that blocks it, and
            # an owed waiter carries a 60s deadline it would burn while its
            # frame sat in this list. The await is also the yield point at
            # which that waiter gets scheduled, so handing the frame back
            # here is what actually pays it. Cleared so the re-injection
            # after the loop cannot put the same frame back twice; anything
            # the waiter did not take is re-read by this loop and retained
            # again, which terminates because the queue only shrinks.
            #
            # This is also why the re-injection after the loop needs no
            # try/finally: _stale_owed is empty at the only interruption
            # point, so the CancelledError below cannot strand a retained
            # frame.
            for _owed in _stale_owed:
                self._queue.put_nowait(_owed)
            _stale_owed.clear()
            # Audit FIRST (off-loop, so it never delays the answer): the
            # reject below is a bounded wire write that can fail, and a
            # permission decision must leave its SEL record either way.
            self._audit_handle_reject(
                stale.id,
                str(_stale_title),
                "stranded_request_pre_turn_drain",
                sub_session_id=(_stale_sid if _stale_sid != self._session_id else ""),
            )
            try:
                await self.reject_tool(stale.id)
                # A stale request belongs to no turn of ours, so its reject
                # says nothing about the steers of the turn starting now.
                self._turn_steering_denied = False
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
                self._runtime._mark_dead("pre-turn drain reject failed")
                continue
            logger.warning(
                "rejected permission request id=%s stranded in the "
                "pre-turn drain (abandoned turn or between-turns child "
                "frame) — answering so the backend cannot hang",
                _loggable_request_id(stale.id),
            )
            # Crew-card notice ONLY for a child-origin strand: the card
            # keys on sub_session_id, so the parent's own session id (an
            # abandoned parent-turn request) can never match a card —
            # emitting it would name the wrong actor. The parent case is
            # covered by the WARNING + SEL record.
            if _stale_sid and _stale_sid != self._session_id:
                self._pending_reject_notices.append((_stale_sid, str(_stale_title)))
        elif stale is not None and is_elicitation_request(stale):
            # An MCP server's elicitation that arrived while no turn was
            # reading (an abandoned turn's tool still running). Like a stranded
            # permission it is a server->client REQUEST the engine waits on, so
            # it is answered -- `cancel`, the answer nobody-was-asked takes --
            # rather than dropped with the frames around it.
            for _owed in _stale_owed:
                self._queue.put_nowait(_owed)
            _stale_owed.clear()
            self._audit_elicitation_decision(
                stale.id, ELICITATION_CANCEL, "stranded_request_pre_turn_drain"
            )
            try:
                await self._runtime.send_response(
                    stale.id, elicitation_response(ELICITATION_CANCEL)
                )
            except asyncio.CancelledError:
                self._queue.put_nowait(stale)
                self._turn_done.set()
                raise
            except Exception:
                logger.exception("failed to answer stranded elicitation request")
                self._runtime._mark_dead("pre-turn drain elicitation cancel failed")
                continue
            logger.warning(
                "cancelled elicitation request id=%s stranded in the pre-turn drain",
                _loggable_request_id(stale.id),
            )
        else:
            # Everything that is not a permission request is DISCARDED, which
            # is correct (it belongs to a turn nobody is reading any more) but
            # was silent. Count it — and CLASSIFY it: a response (``method``
            # is None, ``id`` set — a request can never have ``method`` None,
            # so a terminal cannot reach the branch above) whose result
            # carries a non-empty string ``stopReason`` is by construction
            # the abandoned turn's terminal, read exactly as the live turn
            # reads its own; an ERROR response is terminal-shaped too
            # (_run_turn ends the turn on one), with no stopReason to name —
            # but it is NOT attributed to the abandoned turn, because a late
            # error answer to a concurrently timed-out command call
            # (send_command / compact / set_config_option, re-injected by
            # _wait_for_response's finally) is indistinguishable here. The
            # warning below states the shape, never the owner. The isinstance
            # guard on the leaf mirrors _dispatch.py's wire-stopReason
            # reader: a truthy non-str here would raise on the set membership
            # below, and this arm runs OUTSIDE the _turn_done restoration
            # guard — an escape would wedge the handle permanently.
            _stale_dropped += 1
            if stale is not None and stale.method is None and stale.id is not None:
                _stale_result = stale.result or {}
                _stale_reason = ""
                if isinstance(_stale_result, dict):
                    _stale_reason = _stale_result.get("stopReason", "")
                if isinstance(_stale_reason, str) and _stale_reason.strip():
                    _stale_terminals += 1
                    _stale_cleaned = _stale_reason.strip()
                    if _stale_cleaned in _DRAIN_CLOSED_STOP_REASONS:
                        _stale_reasons.append(_stale_cleaned)
                    elif _stale_cleaned.upper() == STOP_REASON_CONTENT_FILTERED_WIRE:
                        # The one spelling _dispatch.py also normalizes
                        # case-insensitively; log the canonical constant.
                        _stale_reasons.append(STOP_REASON_CONTENT_FILTERED_WIRE)
                    else:
                        _stale_reasons.append("<non-standard>")
                elif stale.error is not None:
                    _stale_terminals += 1

    for _owed in _stale_owed:
        # Order-preserving: the frames go back in the order they were read,
        # and a waiter that has since given up is harmless — the dispatch
        # loop drops a response with no entry in _awaited_responses, and the
        # next drain would too.
        self._queue.put_nowait(_owed)

    if _stale_dropped:
        # One line per turn; count + terminal tally only. The stopReason
        # clause names closed protocol values exclusively (see
        # _DRAIN_CLOSED_STOP_REASONS), DISTINCT values once each (the queue
        # is unbounded, so the clause must not grow per frame — the tally
        # carries multiplicity), and is omitted when there were none — the
        # explicit "0 of them" is the reassuring reading an operator could
        # not get from the old hedge.
        _stale_reason_note = (
            " (stopReason: %s)" % ", ".join(sorted(set(_stale_reasons))) if _stale_reasons else ""
        )
        logger.warning(
            "pre-turn drain discarded %d leftover frame(s) on this "
            "session; %d of them were terminal-shaped responses%s — "
            "those frames reached no consumer",
            _stale_dropped,
            _stale_terminals,
            _stale_reason_note,
        )

    self.last_prompt_stats = self.last_prompt_stats.carry_over()
    # Credits reported between turns belong to no turn's window; this turn
    # bills them (see ``_track_metadata``).
    self.last_prompt_stats.credits += getattr(self, "_between_turn_credits", 0.0)
    self._between_turn_credits = 0.0

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
        # Offer synchronously after the drain, before request building can yield
        # and queue a connected status that makes a reset unsafe. The consent link
        # is consumed by this turn's dispatch loop.
        self._offer_mcp_sign_in()
        # A server that failed for want of a credential is connected again
        # here, after the sign-in offer: the offer takes the runtime's one
        # consent-URL slot first, and a re-attempt never needs it.
        self._reattempt_failed_mcp_servers()
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
        self._prompt_written = True
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
    # A provider failure that codex-acp wrote as text (see
    # ``ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR``). A candidate chunk is
    # HELD, together with a steer-consumed report behind it, until the turn
    # shows what it was: an ``end_turn`` right after it means the turn ended
    # on the adapter's error, and the turn raises that error instead of
    # completing; any other event of this turn releases everything held, in
    # order and unchanged. A slash-command prompt is
    # never held, because its output is unattributed text by design.
    _hold_unattributed = getattr(
        self._runtime, "acp_backend", ""
    ) in ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR and not prompt_is_adapter_command(_params)
    _held: list[AcpEvent] = []
    _dispatch = self._dispatch_events(
        req_id, timeout, extract_command_result=extract_command_result
    )
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
        async for _incoming in _dispatch:
            _out: list[AcpEvent] = [_incoming]
            _failure: dict[str, Any] | None = None
            if _hold_unattributed:
                if is_unattributed_error_candidate(_incoming) or (
                    _held and _incoming.kind == EVENT_STEER_CONSUMED
                ):
                    # Held, not dropped. A steer the turn reports consumed is
                    # held with it: if the turn failed, that steer was not
                    # consumed, and leaving the report out is what lets the
                    # caller queue it again.
                    _held.append(_incoming)
                    continue
                if _held and _incoming.runtime_global:
                    # Another tenant's fanned-out frame on a shared process
                    # says nothing about how THIS turn ended: pass it through
                    # and keep holding.
                    pass
                elif _held:
                    if (
                        _incoming.kind == EVENT_COMPLETE
                        and _incoming.stop_reason == STOP_REASON_END_TURN
                        and not _incoming.synthetic_completion
                    ):
                        _failure = unattributed_terminal_error(
                            [e.text for e in _held if is_unattributed_error_candidate(e)]
                        )
                    if _failure is not None:
                        # The adapter's error is the turn's failure: none of
                        # the held text is delivered as an answer, and the
                        # turn's ``end_turn`` is not delivered as a success.
                        _out = [
                            e
                            for e in _held
                            if not is_unattributed_error_candidate(e)
                            and e.kind != EVENT_STEER_CONSUMED
                        ]
                        _codex_settle = self._settle_codex_compaction(_incoming.stop_reason)
                        if _codex_settle is not None:
                            _out.append(_codex_settle)
                    else:
                        # Anything else after the held text -- more content,
                        # a permission request, another terminal -- means it
                        # was not the turn's failure. Delivered as it was.
                        _out = [*_held, _incoming]
                    _held = []
            for event in _out:
                # Park accounting. The consumer holds this event from here until
                # it comes back for the next one, and that interval is CONSUMER
                # time, not backend silence: the dispatch loop is suspended at
                # its own yield throughout, so its idle clocks would otherwise
                # charge a consumer-side await to the runtime. Measured at this
                # single choke point because `_dispatch_events` yields from 15
                # places and every one of them funnels through this `async for`.
                self._parked_since = time.monotonic()
                if event.kind == EVENT_COMPLETE:
                    # A codex compaction that errors sends no terminal of its own,
                    # so close it out HERE -- before the turn's terminal, so a
                    # consumer that reads a compaction terminal to leave its
                    # compacting state sees it inside the turn it belongs to.
                    #
                    # This site rather than the dispatch loop's own terminals:
                    # ``_dispatch_events`` yields EVENT_COMPLETE from eight places
                    # (including its timeout arm) and every one funnels through
                    # this ``async for``, which is the same reason the park
                    # accounting above is measured here. Settling at each producer
                    # would be eight edits and one of them would be missed.
                    _codex_settle = self._settle_codex_compaction(event.stop_reason)
                    if _codex_settle is not None:
                        yield _codex_settle
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
            if _failure is not None:
                await _dispatch.aclose()
                # The same state the backend's own error response leaves:
                # no stop reason, because the turn did not complete.
                self._last_stop_reason = ""
                _raise_acp_error(
                    _failure,
                    self._advertised_model_ids(),
                    backend=getattr(self._runtime, "acp_backend", ""),
                )
        for event in _held:
            # The dispatch loop returned with no terminal behind the held
            # text, so nothing showed it was a failure: deliver it as text.
            yield event
        _held = []
        # Reached only when the dispatch loop returned on its own — not on a
        # close, a cancel, or an exception.
        _exhausted_clean = True
    except (asyncio.CancelledError, GeneratorExit):
        if not _yielded_terminal:
            # The consumer left before the turn ended (a caller's timeout, a
            # cancelled task, a closed stream). The backend has not been told,
            # so its late frames would reach the next turn on this session.
            self._abandoned_prompt_id = req_id
        raise
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


# ── Internal dispatch ──


async def _close_abandoned_turn(self: AcpSessionHandle) -> None:
    """Cancel the turn a consumer abandoned and wait for its answer.

    ``session/update`` frames carry no request id, so the only boundary
    between the abandoned turn's frames and the next turn's is the abandoned
    prompt's own answer. This sends ``session/cancel`` and waits up to
    ``_CANCEL_GRACE_SECS`` for that answer; :meth:`_wait_for_response`
    re-queues every frame it read on the way, and the pre-turn drain that
    follows discards them. A backend that does not answer within the grace
    costs that one wait, logged, and the turn goes ahead.

    The id is cleared only once the answer arrived or the grace ended. A turn
    cancelled while it waits here, or that fails to send the cancel, leaves
    the id in place, so the next turn closes the abandoned turn again.
    """
    req_id = self._abandoned_prompt_id
    if req_id is None:
        return
    await self._runtime.send_notification(METHOD_CANCEL, {"sessionId": self._session_id})
    try:
        await self._wait_for_response(req_id, timeout=_CANCEL_GRACE_SECS)
    except AcpError as exc:
        logger.info(
            "abandoned turn did not close cleanly before the next prompt (%s)",
            type(exc).__name__,
        )
    if self._abandoned_prompt_id == req_id:
        self._abandoned_prompt_id = None
