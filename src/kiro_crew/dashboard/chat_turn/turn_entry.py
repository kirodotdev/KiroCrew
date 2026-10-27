"""A dashboard turn's opening phase and entry state: ``_turn_open`` (the turn's identity,
provenance, consume veto, allowances and clocks), the Stop signal it reads live, the session's
Stop count, the directive provenance a channel steer can narrow and the start priority of its
cold start (``_turn_start_priority``); and the hand-back of what the turn held on its slot
when it ends (``_turn_release_handles``: the live handles and the batch latch;
``_turn_flush_file_changes``: the file changes)."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.poisoned_escalation import PoisonedConversationEscalation
from kiro_crew.dashboard.state import SUBAGENT_SYNTHESIS_PROMPT, row_mid
from kiro_crew.llm_helpers import _NO_PRIOR_STATS
from kiro_crew.security import StreamRedactor
from kiro_crew.start_priority import person_priority

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        ReplayFamily,
        StartPriority,
        effective_session_key,
        logger,
    )
    from kiro_crew.dashboard.chat_turn.decisions_hooks import _discard_stale_decision
    from kiro_crew.dashboard.chat_turn.file_changes import _flush_file_changes_off_loop
    from kiro_crew.dashboard.chat_turn.native_cards import (
        NATIVE_SUBAGENT_INTERRUPTED_ERROR,
        _native_flush_cut_short,
        _native_subagent_close_all,
        _retain_terminal_native,
    )
    from kiro_crew.dashboard.chat_turn.queue_cycle import _send_chat_done
    from kiro_crew.dashboard.chat_turn.recovery import (
        _rearm_turn_episode,
        _refresh_genuine_turn_allowances,
        _replay_vetoed_at_consume,
        _session_stop_generation_for,
    )
    from kiro_crew.dashboard.chat_turn.segment_flush import _reset_segment_copy
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn, _Flow
    from kiro_crew.dashboard.chat_turn.turn_stats import _turn_clock


def _session_stop_generation(turn) -> int:
    """The session manager's Stop count for this turn's session key."""
    session_key = turn.session_key
    sessions = turn.sessions
    return _session_stop_generation_for(sessions, session_key)


def _directive_producer_is_channel(turn) -> bool:
    """The turn's directive provenance, NARROWED by any channel steer admitted into it.

    ``_directive_channel_origin`` is the opener's provenance and is fixed for
    the turn. A channel human's steer admitted mid-turn
    (``steer_into_running_turn(channel_origin=True)``) sets the slot's
    ``_turn_channel_narrowed`` at admission, before its RPC, and the flag holds
    for the remainder of the turn -- so a directive the model emits after
    channel text reached it is filed as channel-created, never with more
    authority than a channel-origin turn's directive carries. The one reader
    for both directive-application sites, so they cannot disagree.
    """
    _directive_channel_origin = turn._directive_channel_origin
    slot = turn.slot
    return _directive_channel_origin or getattr(slot, "_turn_channel_narrowed", False) is True


def _stop_pressed(turn) -> bool:
    """The user's Stop signal for this turn, read LIVE at the call site.

    True when a stop is in flight OR a monotonic stop generation moved since
    entry -- a Stop that pressed and already resolved back to idle is invisible
    to ``_stopping`` but not to the counters. Two counters are read: the slot's
    own (moved by the dashboard Stop handler) and the session manager's for
    this turn's session key (moved by ``stop_turn`` on any surface). A
    channel-born slot runs its turns on the channel's session, so a stop
    issued from that channel never touches the slot's state -- only the
    session-scoped count sees it. The two end-of-turn continuation gates
    (Stop-hook and refusal recovery) read THIS, never the backend's wire
    stop reason: a backend that aborts a policy-denied turn (codex answers its
    only reject option, ``cancel``, that way) reports ``cancelled`` with no
    Stop pressed, and the continuation is owed there. A function rather than a
    value so no gate can consume a snapshot taken before an await -- the Stop
    hook and the credential-hint lookup both suspend between the turn's end
    and the queue write.
    """
    _responsive_owner = turn._responsive_owner
    _responsive_turn = turn._responsive_turn
    _session_stop_gen_at_entry = turn._session_stop_gen_at_entry
    _stop_gen_at_entry = turn._stop_gen_at_entry
    slot = turn.slot
    return (
        bool(getattr(slot, "_stopping", False))
        or getattr(slot, "_stop_generation", _stop_gen_at_entry) != _stop_gen_at_entry
        or _session_stop_generation(turn) != _session_stop_gen_at_entry
        or (_responsive_turn.owner is _responsive_owner and _responsive_turn.interrupted)
    )


async def _turn_open(turn: _ChatTurn) -> _Flow | None:
    """Open the turn: its identity, provenance, consume veto, allowances and clocks."""
    _current_message = turn._current_message
    _directive_channel_origin = turn._directive_channel_origin
    _directive_self_wake = turn._directive_self_wake
    _directive_user_origin = turn._directive_user_origin
    _prompt_depth = turn._prompt_depth
    _replay = turn._replay
    _replays_completion = turn._replays_completion
    _responsive_protected = turn._responsive_protected
    _synthetic_payload = turn._synthetic_payload
    _synthetic_recovery_turn = turn._synthetic_recovery_turn
    _turn_actor = turn._turn_actor
    _turn_provenance_restored = turn._turn_provenance_restored
    message = turn.message
    slot = turn.slot
    state = turn.state
    turn_exit = turn.turn_exit
    turn_exit.depth = _prompt_depth
    from kiro_crew.dashboard import responsive_chat, steer_retract

    if _prompt_depth == 0:
        responsive_chat.begin_turn(slot, protected=_responsive_protected)
        # Before this turn can be steered: a take-back still offered on a row now
        # belongs to a turn that ended without settling it (a hard Stop, a restart).
        steer_retract.withdraw_stale_offers(state, slot)
    _responsive_turn = responsive_chat.turn_state(slot)
    turn._responsive_turn = _responsive_turn
    _responsive_owner = asyncio.current_task()
    turn._responsive_owner = _responsive_owner
    turn_exit.responsive_owner = _responsive_owner
    _responsive_recovery = _responsive_turn.recovery
    turn._responsive_recovery = _responsive_recovery
    # This turn's message is a sub-agent completion verbatim: dispatched as one,
    # or a recovery entry marked as its verbatim requeue. Runner-written text
    # (the synthesis prompt, a continuation, a retry prompt) never is.
    _turn_is_completion = (
        _turn_actor == "subagent"
        and (not _synthetic_recovery_turn or _replays_completion)
        and not (_synthetic_payload and message == SUBAGENT_SYNTHESIS_PROMPT)
    )
    turn._turn_is_completion = _turn_is_completion

    # Written true the first time this turn's usage row lands, whether at
    # EVENT_COMPLETE or at the abnormal-end seam (_persist_partial_reply), so the
    # two paths cannot both bill one turn and the seam's several callers write it
    # once. Set on the turn object here, before any call reaches the seam that
    # writes it, so every reader sees one value.
    turn._turn_usage_persisted = False

    # The abnormal-end seam (_persist_partial_reply, run from the CancelledError /
    # AcpAuthRequired / AcpProcessDied / AcpError recovery arms) reads these four
    # when it builds the usage row. A cancellation BETWEEN the client's
    # publication (``slot._acp_client = ...``) and these variables' own, later
    # assignment -- a slot close or runtime disconnect during pre-prompt memory
    # and hook work -- unwinds into those arms with the names still unbound, so
    # the seam's keyword list would raise NameError OUT of the recovery handler
    # and strand the error card / session reset it owes. Initialised here, where the body's single
    # entry cannot miss them, and overwritten with their real values in _turn_dispatch_gates,
    # _turn_build_prompt and _on_cancelled; the seam then always reads a bound name.
    _turn_t0 = time.monotonic()
    turn._turn_t0 = _turn_t0
    _stop_reason = ""
    turn._stop_reason = _stop_reason
    slot_ctx_blocks: dict[str, int] = {}
    turn.slot_ctx_blocks = slot_ctx_blocks
    slot_ctx_phase = ""
    turn.slot_ctx_phase = slot_ctx_phase
    slot_ctx_omitted: dict[str, dict[str, Any]] = {}
    turn.slot_ctx_omitted = slot_ctx_omitted
    # The per-turn billing stats observed BEFORE this turn's cancellable work, so
    # the abnormal seam can tell a turn that installed fresh stats from one that
    # died before doing so and still carries the PREVIOUS turn's object. Snapshot
    # once the client is published; the sentinel means "nothing to compare" for a
    # death before even that point. See _persist_abnormal_turn_usage(since=).
    _turn_stats0: object = _NO_PRIOR_STATS
    turn._turn_stats0 = _turn_stats0

    # A decision outcome still pending when a turn STARTS belongs to a turn that
    # has already finished, and one whose own turn produced no assistant row has
    # no reply left to describe. Dropped here rather than on each way a turn can
    # die -- there is one entry and many exits, so the entry is the place that
    # cannot miss one. This turn publishes later (during prompt assembly), so its
    # own outcome is unaffected; see `_decisions_strip_meta` for the claim side.
    _discard_stale_decision(slot)

    # Immutable authority for ORIGINAL replay. ``message`` is enriched later
    # with cancelled-turn preambles, subagent failures, and silent app context;
    # those bytes belong only in the current provider prompt and must never be
    # reclassified or mirrored as authenticated-human speech.
    _incoming_message = message
    turn._incoming_message = _incoming_message

    # An admitted turn does not cancel a return summary: a patrol turn is not
    # the person, and a typed turn already cancelled it through its user row.
    # The summary waits for this turn to end and re-checks what the user typed.

    # Chokepoint invariant: a relay archive NEVER executes. Every dispatch entry
    # point (the primary send, regenerate, edit-resend, rewind, continue,
    # ``session_send``, the queue drain, the OpenAI-compat endpoint) refuses one
    # before reaching here, but they are many and a new one is easy to add. This
    # is the single place that makes running one on this machine impossible
    # regardless of caller. An error row + ``chat_done`` matches the shape a
    # refused turn takes, so the composer unblocks rather than hanging.
    if getattr(slot, "executor", "") == "remote":
        logger.warning("refusing to run relay archive %s", slot.key)
        slot.append(
            "error",
            "This chat ran on a remote crew and is read-only now. Open the "
            "crew's own session to keep going.",
            "msg msg-err",
        )
        # The one exit with no tail: an archive's queue must never drain here.
        turn_exit.handed_off = True
        await _send_chat_done(state, slot)
        return _Flow.END

    # Capture before any await: a Stop can complete while pre-turn setup is
    # suspended and reset _stop_state to idle before continuation processing.
    # The monotonic generation preserves that user intent across the whole call.
    _stop_gen_at_entry = slot._stop_generation
    turn._stop_gen_at_entry = _stop_gen_at_entry
    if _prompt_depth == 0:
        # This turn's directive provenance starts from its OWN opener
        # (`_directive_channel_origin`); a channel steer admitted into the previous
        # turn narrowed that turn, not this one. Reset at the outermost frame only:
        # the depth-1 re-entry runs inside the same turn and must keep a narrowing
        # the turn already took.
        slot._turn_channel_narrowed = False
        slot._turn_channel_origin = bool(_directive_channel_origin)
        slot._turn_user_sent = bool(_directive_user_origin) and not _directive_channel_origin
    # Dispatch appends the triggering row before entering this runner. Freeze
    # that row now, before await points, prompt expansion or new deliveries.
    _current_replay_message = _current_message
    turn._current_replay_message = _current_replay_message
    if _current_replay_message is None:
        _current_replay_message = next(
            (
                m
                for m in reversed(slot.messages)
                if m.get("role") in ("user", "nudge", "subagent", "inject")
            ),
            None,
        )
        turn._current_replay_message = _current_replay_message
        if (
            _current_replay_message is not None
            and _current_replay_message.get("content") != message
        ):
            _current_replay_message = None
            turn._current_replay_message = _current_replay_message

    session_key = effective_session_key(slot)
    turn.session_key = session_key
    sessions = getattr(state, "sessions", None)
    turn.sessions = sessions

    _session_stop_gen_at_entry = _session_stop_generation(turn)
    turn._session_stop_gen_at_entry = _session_stop_gen_at_entry

    # Who caused this turn, for the session ledger. Bound COMPLETELY here, before
    # anything that can fail: ``_queue_recovery`` stamps this value, and the
    # handlers that call it also serve failures raised during pre-turn setup --
    # an ACP cold start that dies, for one. A value refined later would have those
    # recoveries record whatever it held at the time, so an autonudge whose
    # backend died would be retried as a user turn.
    #
    # Structural sources only. ``_directive_self_wake`` is set by
    # ``_fire_dashboard_nudge`` alone, and ``_turn_actor`` comes from a consumed
    # queue entry's enqueue-time kind or from an injector that dispatched this turn
    # itself; a dispatch that named an actor outranks the self-wake flag, since the
    # flag says how the turn was woken and the argument says who it belongs to. A
    # turn no dispatch claimed is a user turn -- never a guess read off the
    # message, which the user writes.
    _crew_log_actor = _turn_actor or ("autonudge" if _directive_self_wake else "user")
    turn._crew_log_actor = _crew_log_actor
    # Local to this invocation, including failures before provider dispatch.
    _recovery_turn_id = uuid.uuid4().hex
    turn._recovery_turn_id = _recovery_turn_id
    turn_exit.actor = _crew_log_actor
    turn_exit.turn_id = _recovery_turn_id
    # This turn's start priority: its cold start and the eager respawn it may arm.
    _turn_priority = _turn_start_priority(
        user_origin=_directive_user_origin,
        crew_log_actor=_crew_log_actor,
        provenance_restored=_turn_provenance_restored,
    )
    turn._turn_priority = _turn_priority
    # Append-only the session's log identity, declared HERE rather than only where
    # it is filled in by _turn_admit and _turn_dispatch_gates: the mid-turn steer
    # cut flushes a segment from the steer handler's task, and a read of one of
    # these before the turn path assigns it would raise instead of simply
    # recording nothing. Each is overwritten once known, and an empty session id
    # or a zero turn already makes every emitter call a no-op.
    _crew_log_sid = ""
    turn._crew_log_sid = _crew_log_sid
    _turn_msg_boundary = 0
    turn._turn_msg_boundary = _turn_msg_boundary
    # True once dispatch has captured this turn's message boundary. Before it,
    # the boundary still reads 0 and would scope a footer write to the WHOLE
    # window, so the stopped-turn footer is gated on it.
    _turn_boundary_set = False
    turn._turn_boundary_set = _turn_boundary_set
    # Identity of the window's tail row when the turn began ("" for an empty
    # window), so the file-change flush can find this turn's rows after a
    # front-trim moved every index. Re-captured at dispatch with the boundary.
    _turn_start_mid = row_mid(slot.messages[-1]) if slot.messages else ""
    turn._turn_start_mid = _turn_start_mid
    # The reply rows this turn appends, recorded by _note_reply_row for the same
    # flush. Fresh per turn: a previous turn whose flush raised would otherwise
    # leave its ids behind, and the flush clears the list itself on exit.
    if isinstance(getattr(slot, "_turn_reply_mids", None), list):
        slot._turn_reply_mids.clear()
    # The turn's ORDINAL for the crew log, kept separate from the message-slice
    # index above even though both start at the same value. The slice index is
    # reset when a mid-turn clear empties the message list, because the turn-stats
    # scan is relative to that list -- and a shared local would silently move the
    # crew log's ordinal to 0 at the same moment, so the turn's own closers would
    # name a turn that never started.
    _crew_log_turn_no = 0
    turn._crew_log_turn_no = _crew_log_turn_no
    _crew_log_step = 0
    turn._crew_log_step = _crew_log_step
    _crew_log_step_t0 = time.monotonic()
    turn._crew_log_step_t0 = _crew_log_step_t0
    # The turn's terminal crew log payload, captured at the terminal event and
    # emitted in _turn_close_crew_log (chat_turn/crew_log_attribution.py): the closers
    # must follow the turn's last flushed assistant message, which happens after
    # _run_chat's stream loop breaks.
    _crew_log_terminal: "dict | None" = None
    turn._crew_log_terminal = _crew_log_terminal
    #: True from ``turn/started`` until this turn's closers are written. What tells
    #: a turn that RAN and must be closed from one the gates refused, which owns a
    #: ``turn/refused`` and needs no closer.
    _crew_log_turn_open = False
    turn._crew_log_turn_open = _crew_log_turn_open
    #: The exception CLASS that ended the turn, when one was caught. Never the
    #: message, which can carry a path or a credential.
    _crew_log_error = ""
    turn._crew_log_error = _crew_log_error

    # Time-to-first-token clock: starts when the user's message reaches the
    # runner, stops at the first visible model output (text OR thinking chunk).
    # This is the end-to-end latency eager spawn / warm pooling exist to cut —
    # startup.duration only covers the handshake slice, so without this the
    # user-perceived win is not measurable. Top-level user prompts only:
    # synthetic payloads and nested prompts are runner-authored, and mixing
    # them in would skew the distribution the feature is judged by.
    _ttft_t0 = time.monotonic() if (_prompt_depth == 0 and not _synthetic_payload) else None
    turn._ttft_t0 = _ttft_t0
    # Persisted twin of the clock above (same start, so queue wait is excluded),
    # stopped at the first broadcast rather than at chunk receipt. A recovery
    # turn may re-send the ORIGINAL payload, so ``_ttft_t0`` is live for the
    # histogram, but the user message it would time from is the earlier turn's:
    # it gets no start of its own and either reuses the carried clock or
    # stores nothing.
    _ttft_visible = _turn_clock(
        slot,
        None if _synthetic_recovery_turn else _ttft_t0,
        top_level=_prompt_depth == 0,
        recovery_turn=_synthetic_recovery_turn,
    )
    turn._ttft_visible = _ttft_visible
    # Same episode boundary for the re-send count: a recovery turn adds to it.
    if _prompt_depth == 0 and not _synthetic_recovery_turn:
        slot._turn_retries = {}
    # Set when a steer cut persists text quietly (no frame): that text becomes
    # visible only with the turn-end ``chat_done`` refresh, so the clock stops
    # there unless a later broadcast stops it first.
    turn._ttft_quiet_text = False

    # Inherit Slack link: if this dashboard session mirrors a Slack thread,
    # copy the link so every exit path, including an auth failure, can reply on
    # the originating surface.
    if sessions is not None and session_key.startswith("dashboard:"):
        link = sessions.get_slack_link(session_key)
        if not (link and link[0]):
            raw_key = session_key[len("dashboard:") :]
            link = sessions.get_slack_link(raw_key)
            if link and link[0] and link[1]:
                sessions.set_slack_link(session_key, link[0], link[1])

    # No pre-turn readiness gate: latched readiness is only refreshed at boot and
    # on explicit user action, so denying here would block a send the CLI would
    # have served. The ACP attempt (_turn_admit's session claim, then _run_chat's
    # stream) is the authority and raises AcpAuthRequired when the CLI is signed out.

    # The agent spec's own hooks, when this session's backend never receives them
    # (filled in once the session client exists; see _prepare_spec_hooks).
    # Declared before _fire, which reads both at call time.
    _spec_hooks: list = []
    turn._spec_hooks = _spec_hooks
    _spec_hooks_unreadable = False
    turn._spec_hooks_unreadable = _spec_hooks_unreadable
    _spec_hooks_cwd: str | None = None
    turn._spec_hooks_cwd = _spec_hooks_cwd

    turn.assistant_text = ""
    # The accumulated text at the last segment cut that was deferred because a
    # construct was open (see open_construct_at_end). While `assistant_text`
    # still starts with it, the pending segment holds text a cut was held back
    # over, even if the construct has closed since, so a compaction reset must
    # finalize it rather than clear it. Any reset of the accumulator breaks the
    # prefix, which retires the marker without each reset site clearing it.
    _deferred_cut_text = ""
    turn._deferred_cut_text = _deferred_cut_text
    # Initialized HERE (not with the other turn-state flags below) because
    # _steer_segment_cut writes it from the steer handler's task, and the turn
    # object must hold it before the cut is published on the slot. Nothing touches it
    # between here and the flag block.
    turn._produced_visible_output = False
    last_heartbeat = time.time()
    turn.last_heartbeat = last_heartbeat
    # Continues the slot's counter rather than starting at 0: seqs are ordered
    # across turns so a client floor from an earlier turn sits below every
    # chunk of this one (see _ChatSlot._chunk_seq).
    turn.chunk_seq = slot._chunk_seq
    in_tool_group = False
    turn.in_tool_group = in_tool_group
    # Rolling-buffer redactor for the live chat_chunk wire stream. Per-chunk
    # redaction misses a credential split across streaming boundaries;
    # this withholds the trailing credential-class run until it is confirmed safe
    # so raw fragments never reach WS/SSE consumers. assistant_text and the
    # slot's raw segment copy (what _flush_segment redacts as one text and
    # saves) are accumulated independently and are unaffected. Flushed where the
    # reply's text ends (_flush_text_stream) and dropped with an abandoned segment
    # (_drop_wire_tail); a tool call or approval prompt inside the reply does
    # neither, see _cut_segment.
    # Deliberately NOT an owner-view seam: this wire stream fans to every
    # connected dashboard client, owner or not, and the finalized text is
    # redacted before it is appended, so the owner's credential-redaction switch
    # cannot apply to chat at all (it applies to the owner's dashboard file
    # viewer only -- see ``security.redaction_switch``).
    _wsred = StreamRedactor()
    turn._wsred = _wsred
    # The runs _wsred holds across a tool call or approval prompt, one per cut
    # crossed before its next output (see _cut_segment / _settle_held).
    turn._held_runs = []
    # Set once the end-of-turn flush has saved ``assistant_text`` as the reply.
    turn._reply_flushed = False
    # The reply rows this turn appended, snapshotted before _flush_file_changes
    # clears the live slot list, so the Slack mirror can still read the whole
    # answer. None until that snapshot is taken.
    turn._reply_mids_snapshot = None

    # Same rolling-buffer protection for the separate chat_thinking wire stream
    # (thinking is broadcast-only / ephemeral, but still real-time on the WS).
    _thinkred = StreamRedactor()
    turn._thinkred = _thinkred

    # Partial-output guard for transient-5xx retry: flipped True once ANY
    # assistant token streams or a tool call fires this turn. A transient
    # backend 5xx is only retried while this is False, so a re-prompt can't
    # double-stream text or re-run a side-effecting tool.
    _turn_emitted = False
    turn._turn_emitted = _turn_emitted
    # Did this turn stream text the user has ALREADY READ, which a later tool
    # boundary then flushed out of `assistant_text`? The three tool-boundary flushes
    # (_on_text_chunk's post-tool-group text in stream_chunks, _on_tool_call in tool_rows,
    # _on_permission_request in tool_approval)
    # persist the segment and reset the buffer, so a turn that answered and THEN
    # hit a blocked tool call reaches end-of-turn with an empty `assistant_text`
    # even though its answer is on screen. Kept separate from
    # `_produced_visible_output`, whose narrower meaning (only the paths that
    # reset the buffer WITHOUT a tool boundary — steer cut, compaction, clear,
    # agent switch) is load-bearing for the promise-only guard in _turn_ending_chain.
    _turn_flushed_visible_text = False
    turn._turn_flushed_visible_text = _turn_flushed_visible_text
    # ── Content-free turn-end diagnostics (empty-response verdict) ──
    # Booleans only, by contract. _turn_settle_answer's empty-response branch reaches its
    # verdict from these, and a WARNING names the cause it derived; every field
    # is safe to log because none of them can carry a prompt, a response, a tool
    # argument, a path, an identity, a token count or a cost. They answer
    # whether a terminal event arrived at all, whether the provider or the
    # backend closed the turn, and whether the turn had done work.
    #
    # `_turn_flushed_visible_text` above and `_turn_tool_calls` / `_turn_thought`
    # below already carry three of the observations, so only what nothing else
    # records is added here.
    _saw_terminal_event = False
    turn._saw_terminal_event = _saw_terminal_event
    # Retained past the EVENT_COMPLETE phase (_on_complete) on purpose: the verdict is
    # reached in _turn_settle_answer after the stream loop, which does not have the event.
    _terminal_synthetic = False
    turn._terminal_synthetic = _terminal_synthetic
    _saw_text_chunk = False
    turn._saw_text_chunk = _saw_text_chunk
    _turn_billed = False
    turn._turn_billed = _turn_billed
    # Was this turn's prompt CONSUMED by the model? Reported to whoever armed the
    # turn (a queued sub-agent completion's retention clock -- see
    # ``_arm_queued_delivery_settlement``), because every handled-failure path
    # of the turn returns from _run_chat NORMALLY and so the call's own return says nothing.
    #
    # Two triggers, and together they are exactly "the announce will NOT be
    # replayed": the provider's own turn-complete event for a real end-of-turn (so
    # an EMPTY response, which consumed the prompt and produced nothing, still
    # counts), and the first streamed token or fired tool call (after which
    # ``build_recovery_requeue`` switches from replaying the prompt to a
    # continuation, i.e. treats it as consumed too). A failure before either --
    # signed-out CLI, dead provider, exhausted prompt-busy retries, transient
    # backend error -- re-queues the prompt itself, and reports nothing.
    #
    # RETRACTABLE for one case only: the FIRST empty response re-queues this exact
    # message verbatim (see _turn_settle_answer's empty-response branch), so the announce is
    # going to be delivered again and the report must be taken back. That decision
    # is only known after the stream ends, which is why this is a retraction rather
    # than a later report. The second empty re-queues a continuation instead, and
    # stays consumed.
    turn._consumed_reported = False
    turn._irreversible_consumption_reported = False

    # The drain validated a claimed replay before spawning the guarded task, but
    # task scheduling creates another revocation window. Re-check the same records
    # at the consume seam so a Stop, correction, steer, or session rebind cannot
    # run a cancelled destructive continuation. Before _run_chat's main try: the exit
    # guard runs this turn's tail.
    if await _replay_vetoed_at_consume(state, slot, _replay, "before_allowances"):
        return _Flow.END

    # Model-activity marker for the poisoned-conversation streak ONLY:
    # flipped True on thinking chunks. Deliberately separate from
    # _turn_emitted — thinking is ephemeral/broadcast-only, so retrying
    # after a thinking-only failure stays safe (see EVENT_THINKING_CHUNK) —
    # but a backend that streams reasoning IS serving this conversation, so
    # such a failure is mid-generation, not the poisoned pre-stream
    # signature, and must not count toward (or survive as) a discard streak.
    _turn_thought = False
    turn._turn_thought = _turn_thought
    # Refresh the one-shot post-token recovery allowance at the START of a
    # GENUINE new user turn, so each real user message gets exactly one recovery.
    # A repeated post-token 5xx that happens DURING recovery must NOT recover
    # again (infinite loop), so the synthetic recovery turn — detected by the
    # incoming message being the recover instruction — deliberately does NOT
    # refresh the allowance and inherits the True flag set when recovery was
    # enqueued. Suppressed/nested recoveries never set the flag, so
    # this reset is a no-op for them and a later real turn can still recover.
    await _refresh_genuine_turn_allowances(
        slot, message, _model_access_replay=ReplayFamily.MODEL_ACCESS in _replay
    )
    # The model-access swap's replay and a queued refusal retry
    # (agent.refusal_fallback_model) replay the user's OWN words, so neither can be
    # recognized by membership in the fixed synthetic-recovery texts above -- and
    # not by TEXT at all. Identity is the drained entry's queue id, claimed by the
    # drain. A user typing the identical text after a Stop purged the replay is a
    # GENUINE turn (fresh episode, fresh allowance) -- identity, unlike text, cannot
    # mistake it for the retry. A cron result binding an unbound slot, a Stop, a
    # steer or a user follow-up landing in the spawn-to-consume window would
    # otherwise replay the user's prompt into a superseding or newly bound session.
    if await _replay_vetoed_at_consume(state, slot, _replay, "after_allowances"):
        return _Flow.END
    _is_refusal_retry_turn = ReplayFamily.CONTENT_FILTER in _replay
    turn._is_refusal_retry_turn = _is_refusal_retry_turn
    await _rearm_turn_episode(
        slot,
        message,
        _is_refusal_retry_turn=_is_refusal_retry_turn,
        _synthetic_recovery_turn=_synthetic_recovery_turn,
        _synthetic_payload=_synthetic_payload,
    )
    # tool_call_id -> DISPLAY TITLE (LLM-authored prose for shell tools; used
    # only for PostToolUse hook name-matching — NOT trustworthy for security).
    _pending_tools: dict[str, str] = {}
    turn._pending_tools = _pending_tools
    # The `ts` of every row ALREADY in this slot when the turn began, so a row
    # this turn appended can be told from one that predates it.
    #
    # It exists because `tool_call_id` alone cannot say which rows belong to the
    # call in front of us: it is the BACKEND's id and session-local, so a reset
    # that recreates the session WITHOUT clearing `slot.messages` -- a project
    # change does exactly that -- leaves a historical row holding an id a later
    # call can be issued again. Scanning `slot.messages` by id then returns that
    # old row beside the new one, and an app flag written from such a scan lands
    # on a row that never had an app. Durably, too: the flag is deliberately
    # never written False, so nothing later clears it, and the notice would draw
    # on the older row while the row that really has the app is passed over.
    #
    # A SET of the pre-existing values, compared by membership, rather than a
    # high-water mark compared by order: `ts` is a string, so an ordering test
    # would need it to sort numerically, and membership needs nothing of the
    # format at all. Snapshotted once per turn here, before any tool row of this
    # turn exists, which is what makes absence from it mean "appended since".
    #
    # Rows of THIS turn are kept whatever their id, so one call owning two rows
    # still works: an auto-approved tool appends a pre- and a post-approval row
    # under one id, both within this turn, and both keep their flag.
    _rows_before_turn: frozenset[str] = frozenset(str(_m.get("ts") or "") for _m in slot.messages)
    turn._rows_before_turn = _rows_before_turn
    # Bounded identity key (`_tcid_identity_key`) -> digest of the FIRST raw id this
    # turn folded into it, plus `_tcid_collapsed` for the keys more than one raw id
    # reached. Credential redaction replaces the whole field with one constant rather
    # than masking a span inside it, so two calls whose ids both look like a
    # credential reach every downstream reader as the same string. A row stores that
    # string, so selecting rows by it would hand one call's row to another call.
    #
    # EVERY id is recorded, including one redaction left alone. An id whose raw
    # value already IS the redaction tag is indistinguishable from a
    # credential-shaped id that redacts to it, so skipping the unchanged ones
    # would leave that pair tracked as a single source and reading unambiguous --
    # and `tool_call_id` comes from the LLM, so that pair is producible.
    #
    # A key identifies a call only when it is IN the first-source map and absent
    # from the collapsed set. Recorded-means-identified rather than
    # absent-means-identified is what bounds this: past `_MAX_TCID_SOURCES` a
    # further key is simply not recorded, and so reads as naming nothing, instead
    # of being accumulated in a second structure that had no bound of its own.
    # `_tcid_collapsed` is therefore a subset of the map's keys and bounded with it.
    #
    # Digests on both sides: distinguishing values is all this needs, a digest keeps
    # the credential-shaped value itself out of the turn's state, and a digest KEY
    # keeps what is retained at 16 characters however long the id was.
    _tcid_first_source: dict[str, str] = {}
    turn._tcid_first_source = _tcid_first_source
    _tcid_collapsed: set[str] = set()
    turn._tcid_collapsed = _tcid_collapsed
    # Identified failed calls only: bounded by the turn's first-source map.
    _failed_write_calls: set[str] = set()
    turn._failed_write_calls = _failed_write_calls
    # tool_call_id -> canonical directive-tool name (forgery gate). Written
    # ONLY at EVENT_TOOL_CALL, ONLY from the out-of-band _meta.kiro identity
    # (event.tool_name + event.mcp_server_name), never from the title. This is
    # the ONLY map the session-directive gate in _on_tool_result (tool_results) trusts.
    _pending_dir_tool: dict[str, str] = {}
    turn._pending_dir_tool = _pending_dir_tool
    # tool_call_id -> digest of the RAW arguments the model called the tool
    # with (``session_directive.call_input_digest`` over ``raw_tool_params``).
    # The out-of-band claim key: the directive tool parked its validated payload
    # under the same digest of the same arguments, so the record is found
    # without reading anything out of the tool RESULT. Recorded for every call
    # that resolves to a directive tool (cheap; no identity needed) and
    # refreshed by a refinement frame carrying
    # rawInput, because some backends stream an empty input on the initial
    # tool_call and the real arguments only on the update.
    _pending_input_digest: dict[str, str] = {}
    turn._pending_input_digest = _pending_input_digest
    # tool_call_id -> the directive tool that call resolved to (see
    # session_directive.directive_tool_from_call); half of the digest above.
    _pending_dir_for_digest: dict[str, str] = {}
    turn._pending_dir_for_digest = _pending_dir_for_digest
    # Identity OBSERVED on each tool_call frame, for the NOT-APPLIED
    # diagnostic only. It cannot be read off the result frame: the
    # tool_call_update path builds its event with no identity fields, so
    # they are always "" there. Two short strings per call, same per-turn
    # lifetime as the maps beside it. Nothing reads this to authorize
    # anything — the grant still comes solely from directive_tool_for at
    # call time.
    _seen_tool_identity: dict[str, tuple[str, str]] = {}
    turn._seen_tool_identity = _seen_tool_identity
    # A worker-tool call the harness never delivered (undelivered_call_watch):
    # built lazily on the first tool call, once the turn's provider is known.
    turn._undelivered_watch = None
    # tool_call_id -> the output we already produced for a CONSUMED directive
    # (applied confirmation, or the native-sub-agent not-applied note). A tool
    # call can surface more than one result frame; once the mapping above is
    # consumed a later frame would otherwise fall through with the RAW marker
    # text and overwrite the applied outcome in the transcript. Replaying the
    # stored output keeps every frame consistent and marker-free.
    _dir_consumed_out: dict[str, str] = {}
    turn._dir_consumed_out = _dir_consumed_out
    # A terminal directive APPLIED this turn (a recorded quiet end —
    # ``session_directive_apply.TERMINAL_DIRECTIVES``) is the turn's
    # intended output. The tool tells the model to end without assistant text,
    # so empty-response recovery must not inject a closing continuation. The
    # signal is the applier's structured ``DirectiveOutcome.ends_turn``, never a
    # match on the outcome prose.
    turn._terminal_directive_applied = False
    turn._terminal_directive_kind = ""
    # The applied quiet end's outcome line (``Nothing new to report.`` plus the
    # model's note). A linked Slack thread has no tool card to read it from, so
    # the reply leg posts it there as the turn's answer.
    turn._terminal_directive_text = ""
    # The tool_call_ids whose directive ENDED the turn. The matching tool rows
    # get ``meta.ends_turn = True`` (persisted and patched live), which is what
    # the interrupted-turn scan reads to tell an applied quiet end from a
    # refused one — both carry the same trusted tool identity.
    _terminal_directive_tcids: set[str] = set()
    turn._terminal_directive_tcids = _terminal_directive_tcids
    # Model activity (text or a tool call) observed AFTER the terminal directive
    # applied: a turn-end contract violation. Only counted, then reported once
    # at turn end — it never fails the turn and never re-arms recovery.
    _activity_after_terminal = 0
    turn._activity_after_terminal = _activity_after_terminal
    # A person's steer entered this turn (admitted into the live stream by
    # ``chat_delivery.steer_into_running_turn``, which registers it in
    # ``slot._pending_steers``). Any text after that may be the model's answer
    # to that person — including text after a quiet end the model reached once
    # it had read the steer — so the post-terminal text drop in _on_text_chunk (stream_chunks)
    # is off for the rest of the turn: a stray bubble is recoverable, a dropped answer is
    # not. The runner's own in-band notices never register in
    # ``_pending_steers``, so they do not lift it.
    _person_steered_turn = False
    turn._person_steered_turn = _person_steered_turn

    # When this turn began, for bounding an out-of-band directive claim to it.
    # A directive belongs to the turn that asked for it: a record parked by a turn
    # that was cancelled before consuming it must not be claimable by a later one.
    _turn_started = time.monotonic()
    turn._turn_started = _turn_started
    # A credential in this turn's reply is traced only to this turn's tools.
    slot.credential_evidence.clear()
    _reset_segment_copy(slot)
    slot.segment_message_id = ""
    # session_id -> {started, done, agent, task} for native kiro-cli subagents,
    # reconciled from `_kiro.dev/subagent/list_update` (one card per sub-agent).
    # The slot holds the same live dict so reconnect snapshots can restore cards.
    _native_tracker: dict[str, dict] = {}
    turn._native_tracker = _native_tracker
    slot._native_subagent_tracker = _native_tracker
    # inner tool_call_id -> native card id, from `_kiro.dev/session/update`, so a
    # sub-agent's tool calls stream onto its own card.
    _native_tc_card: dict[str, str] = {}
    turn._native_tc_card = _native_tc_card
    # Every tool_call_id an activity event names as a native sub-agent's, carded
    # or not: the session-directive refusal reads this, so a child with no card
    # (past a bound, or not listed yet) cannot act on this turn's own session.
    _native_child_tc: set[str] = set()
    turn._native_child_tc = _native_child_tc
    # tool_call_ids whose output was already streamed to a native card — kiro
    # emits two tool_call_update frames per tool (content + rawOutput), so we
    # dedupe to avoid printing the same output twice.
    _native_result_seen: set[str] = set()
    turn._native_result_seen = _native_result_seen
    # native card id -> accumulated activity feed (tool calls + outputs). The
    # published frontend replaces a card's live `streaming` text with `result`
    # on done, so we persist the feed here and send it as the done `result`.
    _native_card_output: dict[str, list[str]] = {}
    turn._native_card_output = _native_card_output
    slot._native_subagent_output = _native_card_output
    _native_card_output_len: dict[str, int] = {}
    turn._native_card_output_len = _native_card_output_len
    needs_session_reset = False
    turn.needs_session_reset = needs_session_reset
    # Poisoned-conversation escalation: unlike needs_session_reset (which
    # preserves the resume sid so the next turn session/loads the same native
    # conversation), discard_conversation CLEARS the sid so the next turn
    # cold-starts a fresh conversation — while keeping the session-map entry,
    # whose Slack thread/channel linkage must survive the recovery. Set only
    # by the poisoned-conversation canary in _on_acp_error_card, the typed
    # unsupported-history-image recovery or the thinking-binding rebuild in acp_recovery.py;
    # their cross-cycle streak and one-shot live in ``_escalation``.
    needs_conversation_discard = False
    turn.needs_conversation_discard = needs_conversation_discard
    _escalation = PoisonedConversationEscalation(slot)
    turn._escalation = _escalation
    _auth_required = False
    turn._auth_required = _auth_required
    saw_compaction = False
    turn.saw_compaction = saw_compaction
    # True once a compaction STARTED notice landed this turn, so _turn_settle_answer's
    # terminal branch can tell "the backend compacted in the middle of this turn" from
    # "no compaction happened". kiro-cli sends started/completed/failed and the
    # claude backend's notices are normalized into the same vocabulary by
    # AcpClient, so this reads identically on both.
    _compaction_started = False
    turn._compaction_started = _compaction_started
    # True only once a compaction reached the COMPLETED terminal. Deliberately
    # narrower than `saw_compaction`, which _broadcast_compaction_result also
    # sets for `failed`: the post-compaction continuation tells the model "the
    # summary above is authoritative", and after a FAILED compaction there is no
    # summary, so continuing on that flag would hand the model a false premise.
    _compaction_completed = False
    turn._compaction_completed = _compaction_completed
    # Assistant-text chunks this turn that were backend CONTROL notices rather
    # than the model's answer. They stay in `assistant_text` and in every
    # downstream path; this list exists only so the post-compaction gate can
    # subtract them (see _answer_text_only) without re-parsing the prose.
    _compaction_notice_chunks: list[str] = []
    turn._compaction_notice_chunks = _compaction_notice_chunks
    _turn_tool_calls = 0  # tool dispatches this turn (refusal diagnostic)
    turn._turn_tool_calls = _turn_tool_calls
    # Calls the backend identified as shell commands this turn. Only such a call's
    # `command` input names a command that ran, so only it may become a credential
    # card's Open in Terminal pre-fill; any other tool's `command` field is data.
    _shell_tool_calls: set[str] = set()
    turn._shell_tool_calls = _shell_tool_calls
    # Every unambiguous coding tool call retains the turn's shared bounded key
    # (`_tcid_identity_key`, a fixed 16-char digest) until its permission
    # decision, so a statusless call that a later denial rejects is never
    # counted. Collapsed ids are withheld, while ids omitted by the shared cap
    # contribute to `_wt_pending_dropped`. A key still held at turn end had no
    # gating denial and counts then.
    _wt_pending_coding: set[str] = set()
    turn._wt_pending_coding = _wt_pending_coding
    turn._wt_coded_this_turn = False
    turn._wt_deciding_coding = False
    _wt_pending_dropped = 0
    turn._wt_pending_dropped = _wt_pending_dropped
    # Monotonic timestamp of the last cadence heartbeat emitted this turn, so
    # confirmation-time emission is throttled to CADENCE_MIN_INTERVAL_SECS and
    # does not flood WakaTime's ~2-minute buckets. 0.0 means none emitted yet.
    turn._wt_last_cadence = 0.0

    # Positive backend provenance for builtin identity. It starts fail-closed and
    # is set only after get_or_create returns the live provider in _turn_admit.
    _builtin_identity_trusted = False
    turn._builtin_identity_trusted = _builtin_identity_trusted
    # One provider-canonical identity tuple per dispatch plus the ids whose final
    # result reported status=completed. Recovery requires exact set equality;
    # model/agent-influenced ACP tool_kind remains telemetry only.
    _turn_tool_identities: list[tuple[str, str, str, bool]] = []
    turn._turn_tool_identities = _turn_tool_identities
    _turn_successful_tool_call_ids: set[str] = set()
    turn._turn_successful_tool_call_ids = _turn_successful_tool_call_ids
    # Snapshot of slot._stop_generation at turn start. `_stop_state` snaps back
    # to "idle" once a Stop resolves, so a Stop pressed AND resolved during the
    # turn is invisible to a point-in-time state check at completion. This
    # monotonic counter records the fact regardless of how quickly it resolved,
    # giving the promise-only guard a turn-window Stop signal.
    # getattr-guarded: the real _ChatSlot always carries it (int), but minimal
    # test stubs may not, and this runs on every path (matching the idiom of
    # `getattr(state, "sessions", None)` above).
    _stop_gen_turn_start = getattr(slot, "_stop_generation", 0)
    turn._stop_gen_turn_start = _stop_gen_turn_start
    _retrying_empty = False
    turn._retrying_empty = _retrying_empty
    # Any empty-response verdict is unlanded for replay durability, including
    # the terminal give-up rung (which intentionally queues no recovery).
    _had_empty_response_verdict = False
    turn._had_empty_response_verdict = _had_empty_response_verdict
    # Set when the turn ended on a promise-only final message and we injected one
    # continuation (see the promise-only guard near turn completion). Like
    # _retrying_empty it suppresses success-recording for this non-landing turn.
    _recovering_promise = False
    turn._recovering_promise = _recovering_promise
    # Set when the context was compacted mid-turn, the turn then ended without
    # finishing the request, and we injected one continuation. Same un-landed
    # semantics as _recovering_promise — and load-bearing for termination: the
    # budget-reset block in _turn_persist_and_settle would otherwise zero
    # `_compaction_continue_retries` on this very turn, un-spending the one-shot
    # and letting a continuation that overflows again recover forever.
    _recovering_compaction = False
    turn._recovering_compaction = _recovering_compaction
    # Set when the turn ended with a tool-call block leaked into its text and
    # the notice was surfaced. Same un-landed semantics as
    # _recovering_promise: the turn announced work it never did, so it must not
    # be recorded as a success or reset the retry budgets.
    _noticed_leak = False
    turn._noticed_leak = _noticed_leak
    # Set when a REAL mid-turn compaction terminal cleared `assistant_text`
    # while it held a leaked tool call. The turn-end gates read that
    # accumulator, so the fact has to be captured at the boundary or it is lost
    # with the text: see `should_notice_compaction_dropped_leak`.
    _compaction_dropped_leak = False
    turn._compaction_dropped_leak = _compaction_dropped_leak
    # Set once ANY leak card has landed this turn. It is what keeps one turn to
    # one leak card, so the dropped-leak notice can sit outside the
    # outcome-owning chain instead of taking an exclusive slot there.
    _leak_card_posted = False
    turn._leak_card_posted = _leak_card_posted
    # Set when the turn's LAST tool result was an infrastructure refusal the
    # recovery ladder chose to retry (L1: the MCP stub's -32001 capacity error
    # or a gateway recoverable_infra marker) and one continuation was queued.
    # Same un-landed semantics as _recovering_promise.
    _recovering_infra = False
    turn._recovering_infra = _recovering_infra
    # Set when the L1 run for this slot ran out of attempts and the turn was
    # given up. Distinct from _recovering_infra, which also marks the turn
    # un-landed and so suppresses turn settlement, budget resets and
    # consolidation: an escalated turn DOES land (nothing is re-queued), it just
    # must not be recorded as the dependency having recovered.
    _l1_escalated = False
    turn._l1_escalated = _l1_escalated
    # Set when a Stop, a steer or a user follow-up arrived DURING the L1 backoff
    # and the re-queue was dropped for it. Same landing semantics as
    # _l1_escalated — the turn ends here — and the same reason to close the run
    # with forget: the retry never ran, so nothing observed the dependency
    # recovering.
    _l1_interrupted = False
    turn._l1_interrupted = _l1_interrupted
    # Whether THIS turn consumed the one-shot post-compaction re-injection flag.
    # Bound at turn scope, not at the consume site: the consume lives inside
    # _turn_build_prompt's context-builder leg, and the probe/base legs skip it entirely —
    # reading an unbound local at the restore in _turn_settle_debts
    # (chat_turn/prompt_assembly.py) would raise UnboundLocalError.
    _needs_reinjection = False
    turn._needs_reinjection = _needs_reinjection
    # Set ONLY where the turn is recorded as successful. _turn_settle_debts restores
    # the re-injection flag when this is still False, which covers every
    # non-landing exit — the early `return`s (stale-recover, tool-stall, error
    # re-queue), every `except` arm, and a hard CancelledError — not just the
    # graceful-cancel and empty-re-queue paths that reach the success check.
    _turn_landed = False
    turn._turn_landed = _turn_landed
    # Turn-scoped so the first-turn-history settle in _turn_settle_debts (which keys on the
    # TERMINAL itself: ``_stop_reason == STOP_REASON_END_TURN``) never raises
    # UnboundLocalError. ``_on_complete`` (turn_stats) assigns ``_stop_reason`` only on
    # EVENT_COMPLETE, but config / memory-prep / stop-before-stream paths exit to
    # teardown before that, so it must be bound up front.
    _stop_reason = ""
    turn._stop_reason = _stop_reason
    # Replay settlement also lives in _turn_settle_debts. Bind at turn scope because
    # config, binding and session-start failures can reach teardown before the
    # acquisition block in _turn_admit determines whether replay is pending.
    _replay_accepted_this_turn = False
    turn._replay_accepted_this_turn = _replay_accepted_this_turn
    # True once a non-slash context-bearing turn ASSEMBLED its history replay
    # (the build arm of ``_context_is_new and not _provider_has_history``).
    # _turn_settle_debts settles the FRESH first-turn history debt only on
    # ``_first_turn_history_assembled`` AND a durably-kept turn (``_turn_landed``
    # on a non-synthetic ``STOP_REASON_END_TURN`` with no empty-response verdict,
    # OR a COMPLETED mid-turn compaction ``_recovering_compaction and
    # _compaction_completed`` — which the landed test cannot see because
    # ``_recovering_compaction`` is itself a guard on the landed-success block)
    # — mirroring the sibling ``provider_switch_replay`` lease. Assembly alone is
    # not enough (a pre-output 5xx fails after assembly and the re-queue needs the
    # debt); a cancelled Stop and an empty-response re-queue are both discarded /
    # re-queued, so neither durably keeps the history and the debt stays armed; a
    # slash command assembles nothing, so assembly must hold too. Bound at turn
    # scope so every teardown path reads a defined value.
    _first_turn_history_assembled = False
    turn._first_turn_history_assembled = _first_turn_history_assembled
    # True while a member DM thread's FIRST turn is in flight: the session
    # client is allocated before the context build, so a build failure (e.g.
    # MemberRulesUnreadable aborting on a malformed rules file) leaves a warm
    # session whose session-start context — the [MEMBER IDENTITY] block,
    # [PERMANENT RULES] included — was never delivered. The generic error arm
    # reads this to re-arm the reinjection flag so the next turn re-delivers
    # the member section instead of running the member with no bounds.
    _member_session_start_pending = False
    turn._member_session_start_pending = _member_session_start_pending
    # Recoverable tool refusals (host-gate policy deny / read-only bash gate)
    # recorded during this turn as (redacted_title, reason). Each deny also gets
    # its reason steered in-band (see _refusal_notices); this ledger is what the
    # FALLBACK continuation carries when that could not be delivered.
    _refusal_reasons: list[tuple[str, str]] = []
    turn._refusal_reasons = _refusal_reasons
    # In-band policy notices steered into THIS turn (see _steer_policy_notice).
    # The list holds only those still unconfirmed: the `steering_consumed` echo
    # settles entries out of it and counts them here instead, so the total ever
    # written stays derivable as list + settled WITHOUT any bookkeeping at the
    # deny sites — a path added later cannot forget to increment a counter.
    _refusal_notices: list[str] = []
    turn._refusal_notices = _refusal_notices
    _refusal_notices_settled = 0
    turn._refusal_notices_settled = _refusal_notices_settled
    return None


async def _turn_release_handles(turn: _ChatTurn) -> None:
    """Hand back the turn's hold on its slot: the live handles and the batch latch."""
    _native_card_output = turn._native_card_output
    _native_tracker = turn._native_tracker
    slot = turn.slot
    state = turn.state
    # Completion can be bypassed by cancellation, provider errors, or timeouts.
    # Close cards idempotently and retain only bounded terminal records for
    # reconnect replay until the next turn installs a fresh tracker.
    # Snapshot which children were still unfinished FIRST — close_all
    # force-marks every card done, and terminal entries linger in the
    # tracker for replay, so reading the tracker afterwards would blame
    # long-completed children for a later ceiling timeout.
    _children_unfinished_final = any(not _i.get("done") for _i in _native_tracker.values())
    # A turn cut short gets no roster that flushes a Claude child's held text,
    # so it goes onto the card here, before the card closes.
    try:
        _native_flush_cut_short(turn)
    except Exception:
        logger.debug("native child text flush at release failed", exc_info=True)
    # Every normal landing already closed its cards in _on_complete, so a
    # card still open here belongs to a turn that was stopped, timed out or
    # crashed before its terminal event.
    try:
        _native_subagent_close_all(
            state,
            slot,
            _native_tracker,
            _native_card_output,
            error=NATIVE_SUBAGENT_INTERRUPTED_ERROR,
        )
    finally:
        slot._native_subagent_tracker = _retain_terminal_native(_native_tracker)
        slot._native_subagent_output = {}
    slot._batch_rejected = False
    slot._batch_rejected_cause = ""
    # Stash the hang-attribution snapshot BEFORE dropping the client ref:
    # if this turn was cut by the dashboard ceiling (_bounded_turn), the
    # done-callback (finish_turn_task) runs AFTER _run_chat's finally, when
    # _acp_client is already None — it reads these two fields to emit
    # kirocrew.turn.timeout.cause for the ceiling path.
    _lc = slot._acp_client
    slot._last_turn_awaiting_permission = bool(
        getattr(_lc, "_awaiting_permission", False)
        or getattr(getattr(_lc, "_handle", None), "_awaiting_permission", False)
    )
    slot._last_turn_children_announced = _children_unfinished_final
    # Steer handle: turn is over, drop the live client ref so a late steer
    # can't target a dead session (the route also re-checks running state).
    slot._acp_client = None
    # Same lifecycle for the segment-cut handle: a late steer must not
    # flush into a finished turn's (already-flushed) locals.
    slot._steer_segment_cut = None
    # Same lifecycle for the child-fidelity opt-in latched at turn start:
    # it is THIS consumer's promise to render the low-fidelity downgrade
    # card. Leaving it set would let a later, fidelity-UNAWARE consumer of
    # the same provider/handle inherit the opt-in and silently disable the
    # handle-level fail-close choke point.
    try:
        setattr(turn.client, "child_fidelity_aware", False)
    except Exception:
        pass
    # The elicitation opt-in is the same promise (to render the card) and
    # ends with the turn for the same reason -- on the client the turn
    # ENDS with, which a recovery phase may have replaced.
    try:
        setattr(turn.client, "accepts_elicitation", False)
    except Exception:
        pass


async def _turn_flush_file_changes(turn: _ChatTurn) -> asyncio.CancelledError | None:
    """Surface the turn's file changes on every exit, cancel and error included.

    The one phase of the ``finally`` before ``_turn_release_session`` that
    suspends: the after-content reads run off the loop. A cancellation landing
    on this await comes back up only after the attach, and is HELD and
    returned rather than let through -- raised here it would pass every
    ``except Exception`` in the turn's finally, skip the session permit release
    and the queue hand-off, and leave the session busy until a restart. The
    caller re-raises it after ``_end_turn_tail`` (which holds its own the same
    way). Any other failure is logged so it cannot skip the re-arm in
    ``_turn_settle_debts`` (chat_turn/prompt_assembly.py) and re-introduce the
    orphan bug this flush prevents.
    """
    slot = turn.slot
    _held_flush_cancel: asyncio.CancelledError | None = None
    try:
        await _flush_file_changes_off_loop(
            slot,
            turn_boundary=turn._turn_msg_boundary,
            turn_start_mid=turn._turn_start_mid,
            turn_had_shell=bool(turn._shell_tool_calls),
        )
    except asyncio.CancelledError as exc:
        _held_flush_cancel = exc
    except Exception:
        logger.debug("_flush_file_changes failed", exc_info=True)
    return _held_flush_cancel


def _turn_start_priority(
    *, user_origin: bool, crew_log_actor: str, provenance_restored: bool
) -> StartPriority:
    """The start priority of a turn's cold start (rule: ``kiro_crew.start_priority``).

    FOREGROUND only for a turn a person sent: authenticated-human provenance this
    process observed, owned by the user -- the same predicate the crew-log gates
    use (``_crew_log_actor == "user" and not _turn_provenance_restored``). That
    covers a typed turn, a person's Resume/Continue press and the runner's requeue
    of a person's failed turn; a self-wake, a dispatch-named actor (cron, sub-agent,
    gateway, app, crew) or a restored queue entry starts BACKGROUND.
    """
    return person_priority(user_origin and crew_log_actor == "user" and not provenance_restored)
