"""What a dashboard turn's session reports about itself mid-turn: a compaction's progress
(``_on_compaction_status``), a native clear (``_on_clear_status``), an agent switch
(``_on_agent_switched``) and its todo list (``_on_todo_update``)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from kiro_crew.dashboard.chat_utils import (
    _broadcast_compaction_result,
    drained_to_thread,
    has_leaked_tool_call,
)
from kiro_crew.dashboard.state import durable_row_count
from kiro_crew.security import redact

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        KiroCrewConfig,
        append_and_surface,
        crew_log_emit,
        logger,
        record_provider_agent_switch,
        refuse_stale_switch,
        restore_agent_selection,
        sel,
    )
    from kiro_crew.dashboard.chat_turn.segment_flush import _cleared_segment_text, _flush_segment
    from kiro_crew.dashboard.chat_turn.turn_admission import (
        _current_binding,
        _require_current_binding,
    )
    from kiro_crew.dashboard.chat_turn.turn_context import _restore_skills_context_after_compaction
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn, _Flow
    from kiro_crew.dashboard.chat_turn.turn_reply_seams import _drop_wire_tail, _flush_text_stream
    from kiro_crew.dashboard.chat_turn.turn_spec_hooks import (
        _prepare_spec_hooks,
        _surface_agent_welcome,
    )


async def _on_compaction_status(turn: _ChatTurn, event) -> None:
    """A compaction's progress the session reports mid-turn."""
    _compaction_completed = turn._compaction_completed
    _compaction_dropped_leak = turn._compaction_dropped_leak
    _compaction_started = turn._compaction_started
    _deferred_cut_text = turn._deferred_cut_text
    _turn_flushed_visible_text = turn._turn_flushed_visible_text
    _wsred = turn._wsred
    saw_compaction = turn.saw_compaction
    slot = turn.slot
    state = turn.state
    logger.debug("Main loop: compaction event text=%r", event.text)
    if event.text == "started":
        # Show the compacting state (input disabled, hourglass) for
        # an AUTOMATIC mid-turn compaction too, not only from the
        # `/compact` branch in _turn_compact_command: a backend that compacts on its
        # own would otherwise leave the UI looking like an ordinary
        # long turn. The terminal notice appended
        # by _broadcast_compaction_result clears the state, and the
        # provider layer guarantees a terminal arrives — the claude
        # backend's automatic compaction has none of its own, so
        # AcpClient synthesizes one at turn end.
        _compaction_started = True
        turn._compaction_started = _compaction_started
        _compaction_text_offset = len(turn.assistant_text)
        turn._compaction_text_offset = _compaction_text_offset
        state.broadcast_ws(
            "chat_message",
            {"slot": slot.key, "role": "compacting", "content": ""},
        )
    # Finalize the accumulated segment BEFORE the terminal notice is
    # appended. _broadcast_compaction_result appends an `assistant`
    # notice row, and _flush_segment's cleanup walks back only to the
    # first non-chunk/non-tool row — so flushing after the notice
    # would stop at it and strand this segment's chunk rows as a
    # duplicate in-memory bubble. Only a segment a cut was deferred
    # over is finalized here, whether or not its construct has closed
    # since: that is the text a deferred cut leaves unflushed, and any
    # other pre-compaction text keeps the plain reset below. The
    # redactor's withheld tail is emitted first so the wire stream
    # carries the whole segment before the reset discards the
    # buffer. The leaked-call scan reads the accumulator here too,
    # since the reset below empties it before the turn-end gates run.
    if (
        not event.synthesized
        and event.text in ("completed", "failed")
        and _deferred_cut_text
        and turn.assistant_text.startswith(_deferred_cut_text)
    ):
        _compaction_dropped_leak = _compaction_dropped_leak or has_leaked_tool_call(
            turn.assistant_text
        )
        turn._compaction_dropped_leak = _compaction_dropped_leak
        _flush_text_stream(turn)
        _flush_segment(state, slot, turn.assistant_text, broadcast=False)
        turn.assistant_text = ""
        _deferred_cut_text = ""
        turn._deferred_cut_text = _deferred_cut_text
        _turn_flushed_visible_text = True
        turn._turn_flushed_visible_text = _turn_flushed_visible_text
        _wsred.reset()
    if _broadcast_compaction_result(state, slot, event):
        saw_compaction = True
        turn.saw_compaction = saw_compaction
        if event.text == "completed":
            _compaction_completed = True
            turn._compaction_completed = _compaction_completed
            _restore_skills_context_after_compaction(turn)
        turn._produced_visible_output = True
        if not event.synthesized:
            # A REAL mid-turn terminal IS a segment boundary: text
            # streamed before it belongs to the window that was just
            # summarized, so it must not carry into the segment
            # flushed afterwards. A SYNTHESIZED terminal is not a
            # boundary — it is manufactured once the turn has ended,
            # so every chunk of the turn already sits in
            # `assistant_text`, and clearing it here would delete the
            # answer a backend produced AFTER compacting.
            #
            # The deferred-text flush ran just above, BEFORE
            # _broadcast_compaction_result appended its notice, so it
            # could remove that segment's chunk rows without stopping
            # at the notice. When it ran, it also emptied
            # `assistant_text` and reset `_wsred`.
            #
            # Scan for a leaked tool call BEFORE the reset: the
            # turn-end leak gates read this accumulator, so a leak
            # that streamed before the boundary is invisible to them
            # once it is cleared. OR-accumulated so a leak dropped at
            # an earlier boundary is not un-reported by a later clean
            # segment. When the hoisted path already emptied the
            # accumulator this reads "" and records nothing new; it
            # is the no-text terminal that relies on the scan here.
            _compaction_dropped_leak = _compaction_dropped_leak or has_leaked_tool_call(
                turn.assistant_text
            )
            turn._compaction_dropped_leak = _compaction_dropped_leak
            _drop_wire_tail(turn)
            turn.assistant_text = _cleared_segment_text(slot)


async def _on_clear_status(turn: _ChatTurn, event) -> None:
    """A native clear the session confirms."""
    _replay_accepted_this_turn = turn._replay_accepted_this_turn
    _replay_pending = turn._replay_pending
    _this_turn_is_clear = turn._this_turn_is_clear
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    # A confirmed native clear is the one destructive slash command:
    # replaying the persisted Kiro Crew history afterwards would undo
    # the user's clear. Retire either an unconsumed slash lease or the
    # consumed-turn marker before any terminal can re-arm it.
    if _replay_pending or _replay_accepted_this_turn:
        state.sessions.commit_provider_switch_replay_sid(session_key)
    if _replay_pending:
        state.sessions.consume_provider_switch_replay(session_key)
        _replay_pending = False
        turn._replay_pending = _replay_pending
    _replay_accepted_this_turn = False
    turn._replay_accepted_this_turn = _replay_accepted_this_turn
    # The FRESH first-turn history debt is armed before the slash
    # branch, and a slash turn never reaches the context-assembly
    # settle, so retire it here too: a fresh `/clear` otherwise leaves
    # the debt armed and the next ordinary prompt rebuilds the very
    # history the user asked to drop.
    state.sessions.consume_first_turn_history_owed(session_key)
    # Advance the durable POSITION base by the rows this clear
    # evicts, exactly as the trim path does (`_ChatSlot.append`)
    # and as every restore path recomputes it. The base plus the
    # window's durable rows is the crew log turn ordinal and the
    # session_control `since` cursor space; emptying the window
    # without crediting the base made the next turn draw an
    # ordinal an earlier turn already wrote (two unrelated turns
    # then read as one turn with contradictory entries) and shifted
    # every cursor down. `durable_row_count` is the ONE shared
    # counting rule, so the base cannot disagree with the ordinal
    # about which rows are durable. Counted BEFORE the clear --
    # afterwards the rows are gone.
    slot._disk_older_durable_count += durable_row_count(slot.messages)
    slot.messages.clear()
    # The boundary was captured against the pre-clear message
    # count; the list is now empty, so reset it to 0 or the
    # clear-confirmation appended below would fall outside the
    # turn-stats scan slice and the completed turn would drop its
    # elapsed/credits stats.
    _turn_msg_boundary = 0
    turn._turn_msg_boundary = _turn_msg_boundary
    _turn_start_mid = ""
    turn._turn_start_mid = _turn_start_mid
    _drop_wire_tail(turn)
    turn.assistant_text = _cleared_segment_text(slot)
    turn._produced_visible_output = True
    # slot_clear FIRST: it wipes the client's message list, so the
    # confirmation row must be delivered after it on every path
    # (append's own broadcast and the reader-suppressed frame alike)
    # or the wipe erases the confirmation it announces.
    state.broadcast_ws("slot_clear", {"slot": slot.key})
    # /clear abandons the plan too. Keeping the pill would make the
    # next cold start rebuild the cleared checklist into the fresh
    # conversation (todo_recovery_prompt), so the person could never
    # shed it.
    # Not on an ownerless frame: a shared runtime fans those out to
    # every peer runner, and a peer's checklist is not what this
    # session's /clear cleared.
    # Gated on THIS turn being the /clear, not on frame ownership: a
    # shared runtime marks the frame ownerless whenever a subagent is
    # registered, which is also true for the session that typed it.
    if _this_turn_is_clear and slot.set_todo(None):
        state.broadcast_ws("todo_update", {"slot": slot.key, "todo": None})
    append_and_surface(state, slot, "assistant", "🗑️ Conversation cleared.", "msg msg-a")


async def _on_agent_switched(turn: _ChatTurn, event) -> Literal[_Flow.BREAK] | None:
    """An agent switch the session reports mid-turn."""
    _spec_hooks: list
    _spec_hooks_cwd: str | None
    _turn_project = turn._turn_project
    client = turn.client
    needs_session_reset = turn.needs_session_reset
    private_member = turn.private_member
    selected_binding = turn.selected_binding
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    new_agent = redact(event.text)
    if new_agent and (private_member or (slot.mode == "member" and new_agent != slot.agent)):
        pinned_member = private_member or slot.agent
        # V2 turns are pinned in every slot mode. Any provider-side
        # switch ends the turn, including a template whose spelling
        # happens to equal the member alias. HTTP guards cannot
        # reach this writer: kiro-cli has
        # ALREADY switched its own session's agent by the time this
        # event arrives. Veto by keeping slot.agent (no broadcast —
        # nothing changed for the UI) and forcing a session reset,
        # so the next turn cold-starts from the slot's bindings on
        # the pinned crew instead of continuing on the switched one.
        logger.warning(
            "agent switch to %r vetoed on member thread %s (pinned to %r)",
            new_agent,
            slot.key,
            pinned_member,
        )
        # SEL: this veto is a permission denial — the one pin
        # enforcement site the HTTP guards cannot reach (kiro-cli
        # already switched) — so it must land in the immutable
        # audit chain like every other member-pin refusal, not
        # only in the mutable process log above.
        sel().log_api_access(
            caller=f"slot={slot.key}",
            operation="chat_runner.agent_switch",
            outcome="denied",
            source="member_pin",
            resources=f"slot={slot.key} agent={new_agent}",
            error=f"member thread pinned to {pinned_member}",
        )
        # The veto must be VISIBLE: kiro-cli has already switched,
        # so the remainder of this turn executes as the foreign
        # agent — on a thread whose whole value is identity, a
        # silent veto reads as the pinned member speaking. Role
        # "notice" (the same channel the runner's other inline
        # banners use) keeps it out of the transcript the model
        # replays as its own prior output.
        slot.append(
            "notice",
            f"📌 Agent switch to {new_agent} was blocked — this thread is "
            f"pinned to {pinned_member}. The next turn restarts on the pinned crew.",
            "msg msg-info",
        )
        needs_session_reset = True
        turn.needs_session_reset = needs_session_reset
        # The vetoed turn DID produce visible output (the notice
        # above) — and more importantly, tool calls completed
        # BEFORE the switch event may have had real side effects.
        # Without this flag the empty-response recovery would
        # requeue the prompt and replay those non-idempotent
        # actions on the reset session.
        turn._produced_visible_output = True
        # Terminate the stream NOW: kiro-cli has already switched,
        # so every further event of this turn — text and tool
        # calls alike — would execute as the foreign agent inside
        # the pinned thread. Returning BREAK ends _run_chat's stream loop and
        # the session reset in _turn_release_session (chat_turn/session_reset.py) tears the
        # switched session down, the same way the tool-rejection paths in
        # _on_permission_prompt bail out of a turn that must not continue.
        return _Flow.BREAK
    elif new_agent:
        # A live provider event can change a template selection.
        # Restore cannot infer that authority from a different
        # transcript name, including a partially persisted switch.
        needs_session_reset = True
        turn.needs_session_reset = needs_session_reset
        turn._produced_visible_output = True
        # The switched-to agent's permissions were fixed when the
        # session started; if they auto-approve what its hooks cover,
        # stop the turn here, before the switch is recorded, so the
        # slot keeps its agent and the reset re-projects the next turn.
        await refuse_stale_switch(client, new_agent)
        _require_current_binding(turn, project_may_be_staged=True)
        switch_cfg = await asyncio.to_thread(KiroCrewConfig.load)
        _require_current_binding(turn, project_may_be_staged=True)
        switch_writer = asyncio.create_task(
            asyncio.to_thread(
                record_provider_agent_switch,
                switch_cfg,
                session_key,
                slot.agent or None,
                new_agent,
                # The project this turn runs in, not a pick staged
                # for the next turn: the switch is resolved and
                # recorded where kiro-cli actually switched.
                _turn_project or None,
            )
        )
        switch_change = None
        switch_cancelled = None
        try:
            while True:
                try:
                    switch_change = await asyncio.shield(switch_writer)
                    break
                except asyncio.CancelledError as exc:
                    if switch_writer.cancelled():
                        raise
                    # Closing the turn cannot abandon a thread that
                    # may still publish a different durable agent.
                    switch_cancelled = exc
            if switch_cancelled is not None:
                raise switch_cancelled
            _require_current_binding(turn, project_may_be_staged=True)
        except (Exception, asyncio.CancelledError):
            await drained_to_thread(restore_agent_selection, session_key, switch_change)
            raise
        slot.agent = new_agent
        # The rest of this turn runs as the new agent, so its tool
        # calls meet the new agent's spec hooks, never the old one's.
        (
            _spec_hooks,
            _spec_hooks_unreadable,
            _spec_hooks_cwd,
        ) = await _prepare_spec_hooks(state, slot, client, new_agent, is_new=False)
        turn._spec_hooks = _spec_hooks
        turn._spec_hooks_cwd = _spec_hooks_cwd
        turn._spec_hooks_unreadable = _spec_hooks_unreadable
        turn._spec_hooks_cwd = _spec_hooks_cwd
        turn._spec_hooks = _spec_hooks
        turn._spec_hooks_unreadable = _spec_hooks_unreadable
        turn._spec_hooks_cwd = _spec_hooks_cwd
        # Re-baseline on the new agent, keeping the turn's own
        # project: a staged pick must not become this turn's.
        _rebased = _current_binding(turn)
        selected_binding = _rebased[:1] + selected_binding[1:2] + _rebased[2:]
        turn.selected_binding = selected_binding
        _drop_wire_tail(turn)
        turn.assistant_text = _cleared_segment_text(slot)
        turn._produced_visible_output = True
        slot.append(
            "assistant",
            f"🔄 Switched to agent: {new_agent}",
            "msg msg-a",
        )
        # The new agent's own greeting, beside the line announcing
        # it. After the switch row so the two read in causal order,
        # and after `slot.agent` is set so the guard is keyed on the
        # agent now active.
        await _surface_agent_welcome(state, slot, new_agent)
        state.broadcast_ws(
            "slot_agent_switch",
            {"slot": slot.key, "agent": new_agent},
        )
    return None


async def _on_todo_update(turn: _ChatTurn, event) -> None:
    """The session's todo list changed."""
    _crew_log_sid = turn._crew_log_sid
    _crew_log_turn_no = turn._crew_log_turn_no
    slot = turn.slot
    state = turn.state
    # Agent's own TODO list. Store on the slot (so /api/chat/slots
    # and the WS `slots` snapshot rehydrate it after a reconnect),
    # then push a lightweight delta so the pill updates mid-turn
    # instead of waiting for the next full slots snapshot.
    if slot.set_todo(event.todo):
        state.broadcast_ws(
            "todo_update",
            {"slot": slot.key, "todo": slot.todo_payload()},
        )
        # Gated on set_todo's own change test, which is what keeps a
        # turn that echoes an identical snapshot on several tool
        # results from writing the same list repeatedly. Inside the
        # gate the entry is a real change to the agent's plan.
        crew_log_emit.on_plan_updated(
            _crew_log_sid,
            _crew_log_turn_no,
            items=(event.todo or {}).get("tasks"),
        )
