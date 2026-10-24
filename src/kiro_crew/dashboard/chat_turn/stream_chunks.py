"""The dashboard turn's stream of text and thinking chunks: each chunk's live redaction (the
live-stream and thinking-stream sinks' redactors) and the segment a text chunk extends
(``_on_text_chunk``, ``_on_thinking_chunk``)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from kiro_crew.dashboard.chat_utils import chunk_generation, open_construct_at_end

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        crew_log_emit,
        logger,
    )
    from kiro_crew.dashboard.chat_turn.segment_flush import _accumulate_segment_raw, _redact_delta
    from kiro_crew.dashboard.chat_turn.turn_ledgers import _report_consumed
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn
    from kiro_crew.dashboard.chat_turn.turn_reply_seams import _cut_segment, _settle_held


async def _on_text_chunk(turn: _ChatTurn, event) -> None:
    """A chunk of the reply's text: its live redaction and the segment it extends."""
    _activity_after_terminal = turn._activity_after_terminal
    _compaction_notice_chunks = turn._compaction_notice_chunks
    _compaction_text_offset = turn._compaction_text_offset
    _crew_log_sid = turn._crew_log_sid
    _crew_log_step = turn._crew_log_step
    _crew_log_step_t0 = turn._crew_log_step_t0
    _crew_log_turn_no = turn._crew_log_turn_no
    _person_steered_turn = turn._person_steered_turn
    _ttft_visible = turn._ttft_visible
    _wsred = turn._wsred
    in_tool_group = turn.in_tool_group
    slot = turn.slot
    state = turn.state
    # If we just exited a tool group, finalize the streaming
    # message so post-tool text starts a fresh message. The wire
    # redactor is NOT flushed: what it still holds from before the
    # tools is joined to this text (see _cut_segment).
    if in_tool_group:
        _open_construct = open_construct_at_end(turn.assistant_text)
        if turn.assistant_text and _open_construct:
            _deferred_cut_text = turn.assistant_text
            turn._deferred_cut_text = _deferred_cut_text
            logger.debug(
                "segment cut deferred on %s — %s still open",
                slot.key,
                _open_construct,
            )
        elif turn.assistant_text:
            _cut_segment(turn)
            _turn_flushed_visible_text = True
            turn._turn_flushed_visible_text = _turn_flushed_visible_text
        else:
            # No accumulated text, but still tell frontend to
            # finalize any streaming message before tools.
            state.broadcast_ws("chat_segment", {"slot": slot.key})
        # Fallback: text after tools means all preceding tools
        # are complete — mark any that weren't already marked
        # (e.g. tools with no output).
        for m in reversed(slot.messages):
            if m.get("role") == "tool" and not m.get("meta", {}).get("done"):
                m.setdefault("meta", {})["done"] = True
                tcid = m.get("meta", {}).get("tool_call_id", "")
                if tcid:
                    state.broadcast_ws(
                        "tool_result",
                        {
                            "slot": slot.key,
                            "tool_call_id": tcid,
                            "output": "",
                            "final": True,
                        },
                    )
            # A `card` row is a change card / guide offer the
            # tool group's own call put into the conversation, so
            # the scan reads past it like the tool rows around it.
            elif m.get("role") not in ("tool", "permission", "chunk", "card"):
                break
        # The same inference for the log. A tool that produced no
        # output sent no result frame, so its call is still open here;
        # without this it stays open for the life of the file.
        #
        # `completed` is passed EXPLICITLY, and only here: the model
        # went on to produce this text, so the tools it was waiting on
        # did finish. The turn-end closers cannot say that and take the
        # emitter's `unknown` default instead.
        crew_log_emit.close_open_tool_calls(_crew_log_sid, _crew_log_turn_no, status="completed")
        # Text after a tool group means the model was called AGAIN:
        # close the call that issued those tools and open the next.
        # This transition is the only per-model-call boundary the
        # stream exposes -- its terminal event is the turn's, not a
        # call's -- so the step is derived here rather than reported.
        crew_log_emit.on_step_completed(
            _crew_log_sid,
            _crew_log_turn_no,
            _crew_log_step,
            ms=int((time.monotonic() - _crew_log_step_t0) * 1000),
        )
        _crew_log_step = crew_log_emit.on_step_started(_crew_log_sid, _crew_log_turn_no)
        turn._crew_log_step = _crew_log_step
        _crew_log_step_t0 = time.monotonic()
        turn._crew_log_step_t0 = _crew_log_step_t0
    in_tool_group = False
    turn.in_tool_group = in_tool_group
    if (
        turn._terminal_directive_applied
        and not _person_steered_turn
        and getattr(slot, "_pending_steers", None)
    ):
        # A person steered into the quiet-ended turn and the
        # backend has not echoed consumption yet (codex reports
        # it only at the terminal): this text may already be the
        # answer to them, so keep every chunk from here on.
        _person_steered_turn = True
        turn._person_steered_turn = _person_steered_turn
    if turn._terminal_directive_applied and not _person_steered_turn:
        # Turn-end contract: the applied terminal directive is the
        # turn's LAST act, so text after it is never the turn's
        # output. It is not accumulated, streamed or persisted: a
        # quiet end renders no assistant bubble, whatever the model
        # goes on to say. The only record is the count reported at
        # turn end. The common source is not the model's own words
        # at all: a quiet end leaves the backend's history on a
        # tool result, the backend's alternation repair fabricates a
        # self-describing assistant placeholder to fill that gap on
        # the next request, and the model reads it back as its own
        # prior reply and repeats it after each later quiet end.
        # Dropping on the structured signal, not the placeholder's
        # wording, covers that and every other late text alike.
        _activity_after_terminal += 1
        turn._activity_after_terminal = _activity_after_terminal
        return None
    # The streamed delta is NOT written to the ledger. Redacting each
    # delta on its own cannot see a credential split across two of
    # them, and token-by-token streaming makes that split the common
    # case -- so the pieces would land in an append-only file that a
    # reader can concatenate. The redacted whole body on
    # ``message/sent`` carries the same content, redacted once over
    # text where the credential is intact and therefore matchable.
    #
    # Pass 4 (`?token=` values) is DEFERRED to that whole-segment run
    # while a companion prefix set is installed: a delta cut inside an
    # approval link shows the pass a `token=` with no prefix before it,
    # so the value is redacted here and the exemption `_redact_segment`
    # would have granted can never apply. Every other pass still runs
    # per delta; the live wire goes through `_wsred`, which never
    # exempts; with the empty default prefix set nothing changes.
    safe_chunk, _removed = _redact_delta(event.text)
    if event.message_id:
        slot.segment_message_id = event.message_id
    _accumulate_segment_raw(slot, event.text, safe_chunk, _removed)
    if not turn.assistant_text:
        _compaction_text_offset = 0
        turn._compaction_text_offset = _compaction_text_offset
    turn.assistant_text += safe_chunk
    if event.control_notice:
        # A backend control notice that arrived as assistant text
        # (the claude adapter's "Compacting..."). It accumulates,
        # streams and persists exactly like any other chunk — the
        # text path is deliberately untouched, because every attempt
        # to special-case it there was swallowed by a later layer.
        # Record it instead, so the post-compaction gate can tell
        # the turn's own ANSWER from a control frame: a notice
        # counted as an answer would shadow the continuation branch
        # and leave the request unanswered — the exact hang this PR
        # exists to fix.
        _compaction_notice_chunks.append(safe_chunk)
    # Set BEFORE the `_turn_emitted` flip: the consumption report
    # below must stay adjacent to that flip (pinned by
    # test_subagent_delivery_ttl_anchor), so a diagnostic flag goes
    # above it rather than between the two.
    _saw_text_chunk = True
    turn._saw_text_chunk = _saw_text_chunk
    _turn_emitted = True  # tokens delivered — transient retry now unsafe
    turn._turn_emitted = _turn_emitted
    await _report_consumed(turn, irreversible=True)
    # Stream to the wire through the rolling buffer so a credential
    # split across token boundaries can't cross a broadcast boundary
    # unredacted. Only the confirmed-safe prefix is emitted;
    # the trailing (possibly-partial-credential) run is withheld until
    # the next chunk or the segment flush. assistant_text above still
    # accumulates the full text for the authoritative final redaction.
    # The first output after a tool call or approval prompt also
    # settles the run held there (see _cut_segment).
    wire = _wsred.feed(event.text)
    if wire:
        wire = _settle_held(turn, wire, continued=True)
    if wire:
        _ttft_visible.mark(wire)
        turn.chunk_seq += 1
        slot._chunk_seq = turn.chunk_seq
        # Same seq and generation on the window row as on the wire
        # frame (see _flush_text_stream) so a mid-stream snapshot
        # carries them.
        row = slot.append("chunk", wire, "chunk")
        row["seq"] = turn.chunk_seq
        row["gen"] = chunk_generation()
        # Push chunk to WS clients (HTTP SSE reader drains from slot._pending)
        state.broadcast_ws(
            "chat_chunk",
            {
                "slot": slot.key,
                "content": wire,
                "seq": turn.chunk_seq,
                "gen": chunk_generation(),
            },
        )


async def _on_thinking_chunk(turn: _ChatTurn, event) -> None:
    """A chunk of the model's thinking: its live redaction."""
    _thinkred = turn._thinkred
    _ttft_visible = turn._ttft_visible
    slot = turn.slot
    state = turn.state
    # Thinking content is not included in the main response text.
    # Broadcast as a separate WS event for frontend rendering.
    # Streamed through StreamRedactor so a credential split across
    # thinking chunks can't cross the wire unredacted;
    # the withheld tail is flushed by _flush_thinking_stream when the
    # thinking phase ends or the turn completes.
    wire = _thinkred.feed(event.text)
    if wire:
        _ttft_visible.mark(wire)
        state.broadcast_ws(
            "chat_thinking",
            {"slot": slot.key, "content": wire},
        )
    # Deliberately NOT a turn-emit: do not flip _turn_emitted here.
    # Thinking is ephemeral, broadcast-only (never persisted to
    # slot.messages and never an irreversible side effect), so a
    # thinking-only turn that then hits a transient backend 5xx is
    # still safe — and worth — retrying. The only cost of a retry is
    # a cosmetic re-stream of reasoning the user already saw; no
    # answer text is doubled and no tool re-runs. If thinking ever
    # starts being persisted/accumulated, this must become a
    # turn-emit (set _turn_emitted = True) to avoid a double-emit —
    # pinned by TestRunChatTransientRetry.test_transient_after_thinking_only_retries.
    # It IS model activity though: the backend demonstrably serves
    # this conversation, so it must break the poisoned-discard
    # streak (a mid-generation death is not the pre-stream
    # rejection signature).
    _turn_thought = True
    turn._turn_thought = _turn_thought
