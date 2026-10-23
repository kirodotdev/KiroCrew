"""What a dashboard turn writes as its reply: the partial reply a dying turn persists, the
abnormal end's usage row, the text and thinking stream flushes, the segment cut at a tool call or
an approval prompt and the run it holds across it, the steer's segment cut, and the error rows the
stream watchdogs post."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        STOP_REASON_CANCELLED,
        TRANSIENT_RETRY_KIND,
        _persist_abnormal_turn_usage,
        chunk_generation,
        crew_log_emit,
        logger,
        provider_last_turn_usage,
        read_turn_model,
        redact,
        reflow_and_label_glued_option_marker,
        save_slot_off_loop,
        slot_history_key,
    )
    from kiro_crew.dashboard.chat_turn.file_changes import _note_reply_row
    from kiro_crew.dashboard.chat_turn.segment_flush import (
        _cleared_segment_text,
        _flush_segment,
        _give_back_run,
        _hold_segment_tail,
        _open_segment_with,
        _redact_segment,
        _segment_row_meta,
    )
    from kiro_crew.dashboard.chat_turn.turn_marker import _gateway_shutdown_requested
    from kiro_crew.dashboard.chat_turn.turn_stats import _attach_owned_turn_stats


async def _persist_partial_reply(turn, reason: str = "") -> None:
    """Persist the partial reply of a turn that is ending abnormally.

    ONE helper for every recovery path, so the log copy cannot be forgotten by
    the next path added -- the same reason redaction lives at the emitter's
    boundary rather than at its call sites. Four handlers had this block
    inline and none of them reached the crew log, so a turn that streamed text
    the user watched got a closer saying it ended and nothing saying what it
    had produced.

    ``_flush_segment`` is deliberately not reused: these paths purge the chunk
    rows first and must not broadcast a segment, and the turn is not
    continuing. What they share with it is the obligation to record the body.
    ``interrupted`` is what makes the entry honest -- this text is what the
    turn had produced when it died, not a reply it finished.

    The usage row is the same obligation at the same seam: a turn ending here
    reached neither ``EVENT_COMPLETE`` (the dashboard's one
    ``persist_token_record_async`` call) nor its ``_attach_turn_stats``, so
    its credits go unrecorded however much it billed. It is written whether or
    not the turn produced partial text -- a turn cut after 91 tool calls bills
    credits with no final assistant segment -- so it sits outside the
    ``assistant_text`` guard, and the client is still live here because the
    ``finally`` that drops ``slot._acp_client`` runs only after this returns.
    The text is saved BEFORE the usage write: the text save is synchronous
    but the usage write awaits a cancellable provider call, and a shutdown
    that interrupts that await must not also drop the body the user watched
    stream. The row is the recoverable loss (a continuation re-reads provider
    stats; the streamed text does not re-materialise), so it goes last.

    Every recovery seam records the row, including the post-token transient
    seam: a transient attempt's spend lives only in the live session's
    ``last_prompt_stats``, which the next prompt ``carry_over()``-zeroes, so
    no continuation can carry it forward and the seam is the only place that
    can bill it. The once-per-turn ``_turn_usage_persisted`` guard keeps a
    second seam on the same turn from double-billing, and the recovery
    continuation is a separate turn with its own fresh stats and guard.
    """
    slot = turn.slot
    state = turn.state
    # Save the interrupted text FIRST, then write the usage row. The text
    # save is synchronous (append + crew-log emit); the usage write awaits a
    # provider call that can be cancelled by a shutdown mid-await. Doing the
    # text save first means a shutdown that interrupts the usage write cannot
    # also drop the body the user watched stream. The usage row is the
    # recoverable loss (a continuation re-reads provider stats; the text does
    # not re-materialise), so it goes last.
    #
    # ``_reply_flushed``: the end-of-turn flush already saved this text and
    # leaves it in ``assistant_text``, so a cancel landing after it (a late
    # Stop, the turn ceiling's task cancel) must not save it a second time.
    #
    # A run the live stream held at the turn's last display boundary is
    # settled first: back on its row, or into this text when it is part of it.
    rows_changed = _drop_wire_tail(turn)
    if turn.assistant_text and not turn._reply_flushed:
        slot.purge_chunks()
        # Same glued-marker repair and whole-text redaction as
        # _flush_segment (both inside _redact_segment): the interrupted body
        # is the same accumulated text, rendered by the same grammar.
        # Records come out of the same pass for the reason _redact_segment
        # gives: after this line only the placeholder exists, so an
        # interrupted reply would otherwise lose what it explains for good.
        _redacted, _blocked, _redactions = _redact_segment(slot, turn.assistant_text)
        _note_reply_row(
            slot,
            slot.append(
                "assistant",
                _redacted,
                "msg msg-a",
                meta=_segment_row_meta(slot, _blocked, _redactions),
            ),
        )
        # Saved once per turn: a recovery arm saves it here, and a cancel
        # landing on the usage write below re-enters this helper.
        turn._reply_flushed = True
        crew_log_emit.on_message_sent(
            turn._crew_log_sid,
            turn._crew_log_turn_no,
            step=turn._crew_log_step,
            text=_redacted,
            interrupted=True,
        )
        rows_changed = True
    if rows_changed and _gateway_shutdown_requested(state):
        # The graceful save ran before these rows changed (this text's row, or
        # a row the held run just went back on, which can happen with no text
        # after the last tool call or approval prompt), and the turn's teardown
        # skips its own save while the process is going away, so without this
        # the change dies with the process. Saved with the in-flight marker
        # still set: the next start shows this text with its interruption row
        # and the Resume button. Before the usage write and the recovery rows
        # below, so the restart reconciles exactly the text the user watched
        # stream.
        try:
            await save_slot_off_loop(
                state,
                slot,
                force=True,
                expected_history_key=slot_history_key(slot),
                expected_slot_name=slot.key,
            )
        except Exception:
            logger.debug("Saving the partial reply at shutdown failed", exc_info=True)
    # The usage row is written whether or not the turn produced partial text
    # -- a turn cut after many tool calls bills credits with no final
    # assistant segment -- so it sits outside the ``assistant_text`` guard.
    await _record_abnormal_turn_usage(turn, reason)


async def _record_abnormal_turn_usage(turn, reason: str) -> None:
    """Write this turn's usage row once when it ends without EVENT_COMPLETE.

    Idempotent across the seam's callers via ``_turn_usage_persisted``: more
    than one recovery handler can fire in a turn's teardown, and the row must
    be written once. ``reason`` is the recovery arm's own classification --
    an ``error:``-shaped reason for a process-died / auth-required / terminal
    AcpError end, and ``cancelled`` only for a genuine Stop press -- so
    ``turn_outcome`` sorts dying runtimes into the fault bucket and keeps
    ``cancelled`` reserved for the one end the operator causes deliberately.
    An involuntary cancel that is not a Stop (tab close, idle-slot sweep,
    shutdown, or the turn ceiling) is labelled the neutral
    ``error: cancelled`` by its arm: telling the ceiling apart from the other
    involuntary cancels is a separate concern no row consumer here needs, so
    there is deliberately no distinct ``timeout:`` ceiling label. A caller
    that cannot name a cause (the plain CancelledError arm reached without a
    Stop press) falls back to the turn's own provider stop reason, then
    ``cancelled``.

    The usage row is attributed from the OUTER provider wrapper (``client``),
    not the inner ``AcpClient`` handle: ``provider_seam`` and the
    context-window read live on the wrapper, so the inner handle alone would
    stamp every row ``provider="acp"`` with a ``(0, 0)`` window. The inner
    handle (``slot._acp_client``) is read only to gate on liveness -- it is
    the ref the ``finally`` drops, so a non-None read means the credits are
    still reachable.
    """
    _ttft_visible = turn._ttft_visible
    session_key = turn.session_key
    slot = turn.slot
    if turn._turn_boundary_set and turn._turn_stats_captured:
        # The turn completed, so EVENT_COMPLETE already measured it and owns
        # its usage row; what it can miss is the footer, because a recovery
        # arm returns before the end-of-turn attach and a cancel (the turn
        # ceiling's task cancel) can land between the two. Attach the
        # completion's own figures here, before the guard below returns.
        _attach_abnormal_turn_stats(
            turn,
            turn._turn_elapsed_ms,
            turn._turn_credits,
            turn._turn_cost_usd,
            turn._turn_model,
            ttft_ms=_ttft_visible.ms,
        )
    if turn._turn_usage_persisted:
        return
    if slot._acp_client is None:
        return
    _elapsed_ms = int((time.monotonic() - turn._turn_t0) * 1000)
    # Read the turn's billing ONCE: ``provider_last_turn_usage`` consumes the
    # accumulated multi-attempt total, so a second read for the footer would
    # see only the last attempt. The row and the footer share this value.
    _usage = provider_last_turn_usage(turn.client, since=turn._turn_stats0)
    if turn._turn_boundary_set and not turn._turn_stats_captured:
        # Every reply an abnormal end leaves is a reply the user read: a
        # Stop press, an error card, a mid-reply 5xx, an involuntary cancel.
        # Give it the footer a completed turn gets, so what the turn cost is
        # visible where it ended. A turn cut before dispatch has no boundary
        # yet, and attaching then would overwrite the previous turn's
        # footer, so it writes only the usage row.
        _attach_abnormal_turn_stats(
            turn, _elapsed_ms, float(_usage.credits or 0.0), float(_usage.cost_usd or 0.0)
        )
    if await _persist_abnormal_turn_usage(
        slot,
        turn.client,
        session_key,
        elapsed_ms=_elapsed_ms,
        stop_reason=reason or turn._stop_reason or STOP_REASON_CANCELLED,
        since=turn._turn_stats0,
        usage=_usage,
    ):
        turn._turn_usage_persisted = True


def _attach_abnormal_turn_stats(
    turn,
    elapsed_ms: int,
    credits: float,
    cost_usd: float,
    model: str | None = None,
    *,
    ttft_ms: int = 0,
) -> None:
    """Put ``meta.turn_stats`` on an abnormally ended turn's last reply row.

    Same writer and the same shape as a completed turn
    (``_attach_turn_stats`` -> ``turn_stats_meta``), so the footer renders
    exactly what it renders for a finished turn: elapsed always, credits or
    cost only when the provider reported a non-zero figure. Scoped to this
    turn's rows by ``_turn_msg_boundary``, which the caller only trusts once
    dispatch has set it; a turn stopped before it wrote any reply row gets no
    footer, because there is no row to carry one.

    The row is mutated in place; it reaches disk because the turn's
    ``finally`` runs ``_flush_file_changes``, which marks the slot dirty on
    every exit. Best-effort: a failure never disturbs the cancel teardown or
    the usage row.

    Only a row THIS runner appended (``slot._turn_reply_mids``) can carry
    it: a workflow or sub-agent result is appended into the same window by
    another writer while the turn runs, and must not take this turn's
    figures. A slot double with no id list falls back to the boundary scan.
    """
    slot = turn.slot
    try:
        reply_mids = getattr(slot, "_turn_reply_mids", None)
        _attach_owned_turn_stats(
            slot,
            elapsed_ms,
            credits,
            cost_usd,
            turn_boundary=turn._turn_msg_boundary,
            model=(read_turn_model(turn.client) or "") if model is None else model,
            ttft_ms=ttft_ms,
            reply_mids=reply_mids if isinstance(reply_mids, list) else None,
        )
    except Exception:
        logger.debug("Failed to attach abnormal-turn stats for slot %s", slot.key, exc_info=True)


def _flush_text_stream(turn) -> None:
    """Emit the redactor's withheld tail as a final chat_chunk before a
    segment is finalized, so WS/SSE viewers see the complete (redacted) text
    and never a truncated stream. No-op when the buffer is empty."""
    _ttft_visible = turn._ttft_visible
    _wsred = turn._wsred
    slot = turn.slot
    state = turn.state
    continued = _held_continued(turn)
    wire = _settle_held(turn, _wsred.flush(), continued=continued)
    if not wire:
        return
    _ttft_visible.mark(wire)
    turn.chunk_seq += 1
    slot._chunk_seq = turn.chunk_seq
    # The window row carries the same seq (and process generation) as the
    # wire frame so a slot snapshot taken mid-stream can tell the client how
    # far the stream it already contains has advanced (see
    # chat_utils._collapse_wire_rows / chunk_generation).
    row = slot.append("chunk", wire, "chunk")
    row["seq"] = turn.chunk_seq
    row["gen"] = chunk_generation()
    state.broadcast_ws(
        "chat_chunk",
        {"slot": slot.key, "content": wire, "seq": turn.chunk_seq, "gen": chunk_generation()},
    )


# A tool call or an approval prompt is a display boundary INSIDE the reply, not
# its end: the text after it can finish a credential the text before it began,
# and two redaction windows cut there emit and save both halves unmatched. So
# the cut there (_cut_segment) leaves the run _wsred still withholds in the
# redactor, where the next text joins it, and off the row it was cut from. The
# first output that holds the run settles it (_settle_held), judged on the raw
# text: a run that redacts together with the raw text after it exactly as the
# two redact apart is a word of its own and goes back on its row; otherwise it
# opens the segment it continued into and is saved with it, redacted as one
# text. One entry per cut crossed before that output (``turn._held_runs``) --
# the row, the raw run cut off it, the variant the row showed then and the
# crew-log model call the row's text came from -- so a run held over several
# cuts goes back piece by piece. The Slack stream carries its held run across a
# `wait` the same way (slack/handler_runtime/stream.py: move_back, _give_back).


def _held_continued(turn) -> bool:
    """Whether text reached _wsred after the run it holds for a boundary."""
    _wsred = turn._wsred
    held = "".join(run for _, run, _, _ in turn._held_runs)
    return bool(held) and (_wsred.discarding or _wsred.held != held)


def _stands_alone(run: str, after: str) -> bool:
    """Whether raw *run* redacts together with *after*, the raw text that
    followed it, exactly as *run* unchanged followed by *after* redacted alone:
    then no value starts in one and ends in the other."""
    return redact(run + after) == run + redact(after)


def _settle_held(turn, wire: str, *, continued: bool) -> str:
    """Settle the run held at a boundary against *wire*, the redactor's next
    output, and return what is left of *wire* for the current segment.
    *continued* says whether text after the boundary reached the redactor too;
    that text is the slot's raw segment copy, which the cut began afresh."""
    slot = turn.slot
    state = turn.state
    if not turn._held_runs:
        return wire
    held = list(turn._held_runs)
    turn._held_runs.clear()
    run = "".join(piece for _, piece, _, _ in held)
    after = slot.segment_raw_text if continued else ""
    if after is not None and wire.startswith(run) and _stands_alone(run, after):
        for row, piece, variant_idx, step in held:
            _give_back_run(state, slot, row, piece, variant_idx)
            _log_given_back(turn, piece, step)
        return wire[len(run) :]
    if not continued:
        # Nothing followed the run, so what it redacts to is its own row's.
        row, _, variant_idx, step = held[0]
        _give_back_run(state, slot, row, wire, variant_idx)
        _log_given_back(turn, wire, step)
        return ""
    turn.assistant_text = _open_segment_with(slot, turn.assistant_text, run)
    return wire


def _log_given_back(turn, text: str, step: int) -> None:
    """Record *text*, given back to a row after its cut, in the crew log.

    The row's ``message/sent`` went out at the cut, ahead of the tool call or
    approval prompt, without the run held then. This entry carries the run under
    the same turn and the same model call (*step*), so a model call's text is
    its ``message/sent`` entries joined in order."""
    crew_log_emit.on_message_sent(turn._crew_log_sid, turn._crew_log_turn_no, step=step, text=text)


def _cut_segment(turn, *, broadcast: bool = True) -> None:
    """Finalize the accumulated text as a segment at a display boundary inside
    the reply, holding back the run _wsred still withholds (see _held_runs).

    Held only while the whole segment saves that run as it was written: a run
    the segment already redacts ends a value it holds whole, so the window
    ends here as it does at the reply's end."""
    _wsred = turn._wsred
    slot = turn.slot
    state = turn.state
    run = "" if _wsred.discarding else _wsred.held
    before = "".join(piece for _, piece, _, _ in turn._held_runs)
    own = run[len(before) :] if run.startswith(before) else ""
    raw = slot.segment_raw_text
    kept = None
    if (
        own
        and raw is not None
        and redact(reflow_and_label_glued_option_marker(raw)[0]).endswith(own)
    ):
        kept = _hold_segment_tail(slot, turn.assistant_text, own)
    if kept is None:
        if not _wsred.discarding:
            _flush_text_stream(turn)
        _flush_segment(state, slot, turn.assistant_text, broadcast=broadcast)
    else:
        row = _flush_segment(state, slot, kept, broadcast=broadcast)
        turn._held_runs.append((row, own, row.get("variant_idx"), turn._crew_log_step))
    turn.assistant_text = _cleared_segment_text(slot)


def _drop_wire_tail(turn) -> bool:
    """Discard what _wsred withholds without streaming it, settling a run it
    holds for a boundary first. Call it BEFORE the segment it drops is
    cleared, so a run that belongs to that segment is dropped with it.

    True when it settled such a run: back on its row, which changes a row
    already in the window, or into the current segment's text."""
    _wsred = turn._wsred
    settled = bool(turn._held_runs)
    if settled:
        continued = _held_continued(turn)
        _settle_held(turn, _wsred.flush(), continued=continued)
    _wsred.reset()
    return settled


def _flush_thinking_stream(turn) -> None:
    """Emit the thinking redactor's withheld tail when the thinking phase
    ends (any non-thinking event) or the turn completes. No-op when empty."""
    _thinkred = turn._thinkred
    _ttft_visible = turn._ttft_visible
    slot = turn.slot
    state = turn.state
    wire = _thinkred.flush()
    if wire:
        _ttft_visible.mark(wire)
        state.broadcast_ws("chat_thinking", {"slot": slot.key, "content": wire})


def _steer_segment_cut(turn, *, interrupted: bool = True) -> None:
    """Finalize the accumulated text as a segment at a mid-turn steer.

    ``interrupted=False`` is the crewmate send that waits behind this turn
    (``responsive_chat.accept``): the segment is cut so the person's row
    lands where they sent it, but the reply goes on, so it is not marked
    cut off.

    Published on the slot (next to ``_acp_client``) so the dashboard steer
    handler can cut the segment right BEFORE it persists the steer user
    message. Without the cut, kiro-cli keeps the segment open across the
    steer, so ``_flush_segment`` at end-of-segment appends the WHOLE text
    (pre-steer + post-steer) BELOW the steer bubble — the reply the user
    watched stream above their steer jumps to the bottom when the chat_done
    refresh rebuilds from server history — and the pre-steer ``chunk``
    entries are stranded above the bubble forever (the trailing-run walk in
    ``_flush_segment`` stops at the first non-chunk message).

    ``broadcast=False``: the initiating tab already froze its streaming
    message when it pushed the optimistic bubble, and other tabs freeze on
    the ``steer_push`` echo (appendSlotMessage finalize-on-steer). A
    chat_segment broadcast here could instead finalize a NEWER post-steer
    streaming message that raced ahead on the initiating tab.

    Sync on purpose — the handler and this turn share the event loop, so
    the flush cannot interleave with chunk processing.
    """
    slot = turn.slot
    state = turn.state
    # Drop the wire redactor's withheld tail instead of emitting it: a
    # chat_chunk broadcast here would arrive AFTER the clients froze their
    # streaming message at the steer boundary, opening a phantom streaming
    # bubble below the steer card. No text is lost — assistant_text
    # accumulates the full segment independently of the wire buffer, and
    # _flush_segment persists (and re-redacts) that full text. A run held at
    # an earlier tool call is settled into its row or into this segment.
    _drop_wire_tail(turn)
    if turn.assistant_text.strip():
        # The persisted row may hold text the redactor never broadcast (a
        # fully withheld first chunk). It is published quietly, so it is
        # timed at the turn-end refresh, not here.
        turn._ttft_quiet_text = True
        # quiet_persist: the clients already hold this text in their
        # frozen (pre-steer) message; the append's chat_message broadcast
        # would render a duplicate copy below the steer bubble.
        #
        # `interrupted`: this text was CUT OFF, and this site is the one that
        # knows it -- it is cutting the segment precisely because a steer
        # arrived. Without the mark the log cannot tell a reply the user
        # interrupted from one the model finished. It needs no coordination
        # with the echo that proves consumption, which is why this fact is
        # recordable where a steer entry of its own is not.
        _flush_segment(
            state,
            slot,
            turn.assistant_text,
            broadcast=False,
            quiet_persist=True,
            interrupted=interrupted,
        )
        # The flushed segment IS visible output. Every other mid-turn site
        # that resets assistant_text (compaction, clear, agent switch,
        # /compact) sets this flag too; without it, a turn that streamed
        # text, got steered, and then ended with no further text would hit
        # the empty-response branch and requeue the ORIGINAL prompt —
        # re-running its side effects.
        turn._produced_visible_output = True
    # The flush consumes the raw copy; a blank segment does not.
    turn.assistant_text = _cleared_segment_text(slot)


def _emit_stale(turn, msg: str, *, will_retry: bool = False) -> None:
    slot = turn.slot
    slot.append(
        "error",
        msg,
        "msg msg-err",
        meta={"kind": TRANSIENT_RETRY_KIND} if will_retry else None,
    )
    # No explicit chat_message: slot.append already emits ONE, and it
    # carries `meta` -- a second frame here would arrive untagged.


def _emit_stall(turn, msg: str, *, will_retry: bool = False) -> None:
    slot = turn.slot
    slot.append(
        "error",
        msg,
        "msg msg-err",
        meta={"kind": TRANSIENT_RETRY_KIND} if will_retry else None,
    )
    # No explicit chat_message: slot.append already emits ONE, and it
    # carries `meta` -- a second frame here would arrive untagged.


def _emit_error(turn, msg: str, *, will_retry: bool = False) -> None:
    slot = turn.slot
    slot.append(
        "error",
        msg,
        "msg msg-err",
        meta={"kind": TRANSIENT_RETRY_KIND} if will_retry else None,
    )
    # No explicit chat_message: slot.append already emits ONE, and it
    # carries `meta` -- a second frame here would arrive untagged.
