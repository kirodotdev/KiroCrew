"""What a tool's result does to a dashboard turn: its row, its file changes, the session directive
it carries (decode, apply, strip), the MCP-app render it claims and the follow-ups it arms."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        HOOK_EVENT_POST_TOOL_USE,
        TERMINAL_TOOL_STATUSES,
        _git_repo_touches,
        _redact_tool_field,
        append_and_surface,
        apply_session_directive_outcome,
        crew_log_emit,
        directive_queue,
        effective_session_key,
        logger,
        mcp_apps_render,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
        session_directive,
    )
    from kiro_crew.dashboard.chat_turn.deny_chokepoints import _steer_repeat_loop_notice
    from kiro_crew.dashboard.chat_turn.directives import (
        unverified_directive_notice,
        unverified_directive_outcome,
    )
    from kiro_crew.dashboard.chat_turn.file_changes import (
        _failed_write_entries,
        _forget_entries,
        _settle_failed_write,
    )
    from kiro_crew.dashboard.chat_turn.native_cards import _append_native_output
    from kiro_crew.dashboard.chat_turn.recovery import _notice_compaction_after_turn
    from kiro_crew.dashboard.chat_turn.tool_rows import (
        _persist_tool_result_rows,
        _tcid_identity_key,
    )
    from kiro_crew.dashboard.chat_turn.turn_entry import _directive_producer_is_channel
    from kiro_crew.dashboard.chat_turn.turn_ledgers import (
        _record_terminal_directive,
        _wt_maybe_cadence,
    )
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn
    from kiro_crew.dashboard.chat_turn.turn_spec_hooks import _fire


async def _on_tool_result(turn: _ChatTurn, event) -> None:
    """A tool's result: its row, file changes, session directive, MCP-app render and what it arms."""
    _compact_after_turn_noticed = turn._compact_after_turn_noticed
    _crew_log_sid = turn._crew_log_sid
    _crew_log_turn_no = turn._crew_log_turn_no
    _deferred_cut_text = turn._deferred_cut_text
    _dir_consumed_out = turn._dir_consumed_out
    _directive_loop_id = turn._directive_loop_id
    _directive_self_wake = turn._directive_self_wake
    _directive_user_origin = turn._directive_user_origin
    _failed_write_calls = turn._failed_write_calls
    _native_card_output = turn._native_card_output
    _native_card_output_len = turn._native_card_output_len
    _native_result_seen = turn._native_result_seen
    _native_tc_card = turn._native_tc_card
    _pending_dir_for_digest = turn._pending_dir_for_digest
    _pending_dir_tool = turn._pending_dir_tool
    _pending_input_digest = turn._pending_input_digest
    _pending_tools = turn._pending_tools
    _repeat_loop = turn._repeat_loop
    _rows_before_turn = turn._rows_before_turn
    _seen_tool_identity = turn._seen_tool_identity
    _shell_tool_calls = turn._shell_tool_calls
    _tcid_collapsed = turn._tcid_collapsed
    _tcid_first_source = turn._tcid_first_source
    _terminal_directive_tcids = turn._terminal_directive_tcids
    _turn_actor = turn._turn_actor
    _turn_started = turn._turn_started
    _turn_successful_tool_call_ids = turn._turn_successful_tool_call_ids
    _wt_pending_coding = turn._wt_pending_coding
    client = turn.client
    is_slash = turn.is_slash
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    if event.tool_final and event.tool_call_id:
        _turn_successful_tool_call_ids.add(event.tool_call_id)
        # An auto-approved coding tool reaches its completed frame
        # without ever passing through a permission decision, so
        # _wt_note_approved never emitted a cadence beat for it and
        # its key is still pending (an approved tool's key was
        # drained at EVENT_PERMISSION_REQUEST and already got its
        # beat). Emit the throttled beat here, where the tool has
        # demonstrably executed, so a turn of pre-authorized coding
        # tools accrues intra-turn duration like a prompted one. The
        # key stays pending for the end-of-turn attribution beat;
        # this only adds the mid-turn timestamp. Same throttle and
        # scope guard as the per-approval path.
        _wt_result_key = _tcid_identity_key(event.tool_call_id)
        if _wt_result_key and _wt_result_key in _wt_pending_coding:
            _wt_maybe_cadence(turn)
    _out = _redact_tool_field(event.tool_output)
    # Redact the join key once for the WS broadcast and the
    # message-meta comparison below. `_tool_meta` stores the
    # redacted form, so the comparison must use the redacted form
    # too — see the `_tool_meta` docstring for the convention.
    _tcid = _redact_tool_field(event.tool_call_id) if event.tool_call_id else ""
    # Whether that value still NAMES one call. It cannot be answered by
    # re-redacting: _run_chat's stream-loop preamble already replaced
    # `event.tool_call_id` with the redacted form for every event, so
    # comparing the two here compares a value with itself and is true
    # even for a collapsed id. The turn records an identity key at that
    # preamble, while the raw id still exists, and `_tcid_collapsed` holds
    # the keys more than one distinct raw id reached. The key is derived
    # here with the SAME helper, so both ends agree by construction; a key
    # that was never recorded -- an over-long id, or one past the tracking
    # bound -- names nothing and is withheld.
    #
    # Where this is false, the durable "an app was here" claim is
    # withheld -- see `_owned_rows` below and the flag write further
    # down. Both fail towards silence, which a later render repairs; a
    # flag persisted onto an unrelated row does not, because it is
    # never written False and the client draws its notice on the first
    # flagged row it finds for the id. `done` and `output` keep the
    # id-wide walk they already had: they describe the CALL, not a
    # claim about which row once held an app.
    #
    # Evaluated at the moment of the write, which is sound because a
    # row can only be wrongly selected if it already exists, and a row
    # exists only if its own call event passed that preamble and was
    # counted there.
    _tcid_identifies = (
        bool(_tcid)
        and bool(_tcid_row_key := _tcid_identity_key(event.tool_call_id))
        and _tcid_row_key in _tcid_first_source
        and _tcid_row_key not in _tcid_collapsed
    )
    # Every TERMINAL frame, not only the successful one. `tool_final`
    # is true for `completed` alone -- that is what the transcript
    # paths need, and changing it would change what they credit and
    # finalise -- but a durable record cannot skip a tool that FAILED:
    # its call reached a terminal state, and leaving it open means the
    # turn-close sweep later records `unknown` for an outcome the
    # stream actually reported. `stop_reason` on this event describes
    # the turn, not the tool, so it is not used here.
    _tool_terminal = event.tool_final or (event.tool_status in TERMINAL_TOOL_STATUSES)
    _not_written = []
    if (
        _tool_terminal
        and _tcid_identifies
        and (event.refusal or event.tool_status in ("failed", "refused"))
    ):
        _failed_write_calls.add(_tcid_row_key)
        _not_written = _failed_write_entries(slot, _tcid_row_key)
        # Cancellation at any await must leave only paths until a
        # completed content check proves what the call changed.
        for _held in _not_written:
            _held["unverified_write"] = True
    _loop_notice = _repeat_loop.note_result(
        event.tool_call_id,
        status=event.tool_status,
        output=event.tool_output,
        output_digest=event.tool_output_digest,
        terminal=_tool_terminal,
    )
    if _loop_notice:
        await _steer_repeat_loop_notice(client, _redact_tool_field(_loop_notice))
    if event.tool_output_credentials and _tcid_identifies:
        # The call's own input (already redacted) names the source;
        # the fingerprints say which credentials it produced. An id
        # that collapsed onto several calls names none of them, so
        # its result records no source rather than borrowing another
        # call's input and shell status.
        for _row in reversed(slot.messages):
            if _row.get("role") == "tool" and _row.get("meta", {}).get("tool_call_id") == _tcid:
                slot.credential_evidence.record(
                    str(_row["meta"].get("input") or ""),
                    event.tool_output_credentials,
                    is_shell=event.tool_call_id in _shell_tool_calls,
                )
                break
    if _tool_terminal:
        _git_repo_touches.finish_tool_call(
            slot,
            event.tool_call_id or "",
            executed=event.tool_final and not event.refusal and _tcid_identifies,
            background_tasks=getattr(state, "_background_tasks", None),
        )
        # The backend's own word, unmapped, with the refusal this
        # process decided taking precedence -- a refused call never
        # reached the tool, whatever the frame says.
        _tool_status = (
            "refused"
            if event.refusal
            else (event.tool_status or ("completed" if event.tool_final else "unknown"))
        )
        crew_log_emit.on_tool_completed(
            _crew_log_sid,
            _crew_log_turn_no,
            name=event.tool_name or "",
            server=event.mcp_server_name or "",
            status=_tool_status,
            call_id=event.tool_call_id or "",
            is_error=event.refusal is not None or _tool_status == "failed",
            result=event.tool_output or "",
            result_digest=event.tool_output_digest,
            result_bytes=event.tool_output_bytes,
        )
        # A dispatched worker's report the harness never delivered (KAS answers a
        # name it cannot resolve without calling the server): record it beside the
        # worker's item so its conductor and the board learn the report did not
        # arrive, instead of reading the silence as a stall.
        if turn._undelivered_watch is not None:
            await turn._undelivered_watch.on_result(event)
        _digest_backed = [held for held in _not_written if held.get("pre_call_digest")]
        if _digest_backed:
            _same, _changed = await asyncio.to_thread(_settle_failed_write, _digest_backed)
            _forget_entries(slot, _same)
            for _held in _changed:
                _held.pop("unverified_write", None)
    # MCP Apps (flag-independent on this side): if gatewayd spooled a
    # UI payload it injected an opaque marker into the result text.
    # Load it, push an mcp_app_render event to this slot, and strip
    # the marker from the transcript text (cosmetic, like redaction).
    # Awaited: the spool read inside is thread-offloaded (multi-MB
    # records must not stall this event loop).
    # Rows this call already owns, by their own `ts`. Resolved HERE,
    # synchronously, so the claim below can be attributed to a row
    # identity rather than to the backend's `tool_call_id`: a `ts`
    # is minted per row, where a session reset can reissue a
    # `tool_call_id` an older row already used. A `ts` is not an
    # identity: an explicit one is preserved verbatim for a replayed
    # channel row, and a coarse clock stamps two rows appended in one
    # tick alike, which is the reason `meta.mid` is minted at all. Reading `slot.messages` adds no
    # await, so the claim stays adjacent to the checks above it.
    #
    # Restricted to rows THIS TURN appended. Matching on the id
    # alone would also return a historical row a transcript-
    # preserving reset left holding the same id, which would put
    # that old row's `ts` into the claim and let the old row answer
    # for this call's app.
    #
    # Gated on the id still identifying one call: a redacted id is
    # one constant shared by every redacted id, so it would select
    # an unrelated same-turn row as an owner. Empty here is an
    # already-handled case rather than a failure -- the claim is
    # still written for its render-once half and simply attributes
    # nothing, which is what `handle_tool_result` documents for a
    # claim whose row list is empty.
    _owned_rows: list[dict] = (
        [
            m
            for m in slot.messages
            if m.get("role") == "tool"
            and m.get("meta", {}).get("tool_call_id") == _tcid
            and str(m.get("ts") or "") not in _rows_before_turn
        ]
        if _tcid_identifies
        else []
    )
    # Both identities are derived from ONE row list so they cannot
    # drift apart. `meta.mid` is what the claim is ATTRIBUTED by: it is
    # minted per row, so it separates two rows a coarse clock stamped
    # in the same tick, and a row replayed from a channel transcript
    # whose own `ts` was kept verbatim. The `ts` rides along as the
    # fallback identity for a claim body carrying no ids, and is
    # compared as a set rather than for order.
    _owned_row_ts: list[str] = [str(m.get("ts") or "") for m in _owned_rows]
    _owned_row_ids: list[str] = [str(m.get("meta", {}).get("mid") or "") for m in _owned_rows]
    _render = await mcp_apps_render.handle_tool_result(
        state,
        slot_key=slot.key,
        tool_call_id=_tcid,
        text=_out,
        # WS routes on the bare slot.key, but the gateway recorded
        # the CANONICAL producing session on the spool. Pass it for
        # the binding check or every real render is refused as a
        # bare-vs-prefixed mismatch (silent no-render).
        producing_session_key=effective_session_key(slot),
        row_ts=_owned_row_ts,
        row_ids=_owned_row_ids,
        # The claim is durable the moment it is taken; these rows are
        # not, until a flush writes them. `rows_only` writes the
        # transcript window and leaves the metadata line alone, which
        # is all the claim needs to be attributable after a restart.
        # This does NOT clear `_dirty` -- only the flush passes do --
        # so the `mcp_app_lead` flag written further below is still
        # owed to the periodic flush exactly as before.
        persist_rows=lambda: _persist_tool_result_rows(state, slot),
    )
    _out = _render.text
    # Rows this frame flags, by their own `ts`. That is enough HERE,
    # because the flag write is in-memory against the very rows just
    # selected. It is not enough to identify a row LATER: append
    # keeps an explicit `ts` verbatim for a row replayed from a
    # channel, and a coarse clock stamps two appends in one tick
    # alike, which is the collision `meta.mid` is minted to answer --
    # so the claim records the mids and is attributed by them. A
    # tool_call_id identifies even less: an auto-approved call has a
    # pre- and a post-approval row sharing the id, and the client's
    # patch reducer takes only the newest of those.
    _app_flag_rows: list[str] = []
    # The render payload is LIVE-ONLY by design (owner-scoped WS, a
    # callback capability, and a record that expires), so a reload
    # has nothing to rebuild the frame from. Remember that this row
    # HAD an app and persist it below, so a reader is told the app
    # exists instead of seeing nothing at all. True as well when the
    # claim was already spent, which is how a row recovers after a
    # gateway death between the claim and this flag reaching disk.
    _app_on_row = _render.app_on_row
    # Session directive: a stateless session-bound tool
    # (monitor_start / monitor_update / autonudge_stop / set_project
    # / suggest_followup / ask_question) returns a directive marker
    # instead of resolving its own session identity. Apply it HERE,
    # where slot.key + session_key are the AUTHORITATIVE session for
    # this turn, then record the applier's real outcome on Kiro Crew's
    # OWN surfaces (transcript / WS / hooks) and drop the marker.
    # NOTE: gateway-off (the default), the MODEL already received the
    # tool's own return over the MCP pipe — this does NOT rewrite the
    # model's tool result, which is why the tool's own message is
    # written to not over-claim the (consumer-applied) effect.
    # Gated on _pending_dir_tool — the CANONICAL _meta.kiro tool name
    # for a genuine MCP call, NOT model-authored result/title text —
    # so a forged marker under a shell/non-directive tool is ignored.
    # A native sub-agent's tool calls DO surface here (flat events
    # tagged in _native_tc_card) but have no independently bindable
    # slot, so they are refused rather than applied to the parent —
    # combined with spawn_run sub-agents running their own loop, no
    # sub-agent can ever arm/mutate its parent (isolation).
    _dir_tool = _pending_dir_tool.get(event.tool_call_id, "")
    # DIAGNOSTIC ONLY (never a grant): a marker arrived under a call
    # the identity gate did not record. That is the correct outcome
    # for a forged shell result, and ALSO what a backend which emits
    # no ``_meta.kiro`` produces for a genuine directive tool — and
    # the gate returns "" with no log, so the two were indistinguish-
    # able and the second looked like nothing happening at all. Log
    # the recorded identity so an operator can tell them apart.
    #
    # Entered on a marker OR on a parked record for this session whose
    # call this frame could be: the record is claimed by the call's
    # input digest, and the result body is not consulted -- a backend
    # that caps or offloads the result loses the marker entirely, and
    # the directive must still land. The depth probe keeps an ordinary
    # tool result (nothing parked) out of this branch at dict-lookup
    # cost, so the diagnostics below only ever run for a frame that
    # is directive-shaped on at least one channel.
    _in_digest_probe = _pending_input_digest.get(event.tool_call_id, "")
    if (
        (
            not _dir_tool
            or (
                event.tool_final
                and session_directive.decode(_out, _dir_tool) is None
                and not session_directive.is_refusal(_out)
            )
        )
        and event.tool_call_id not in _dir_consumed_out
        and (
            session_directive.has_marker(_out)
            or (event.tool_final and _in_digest_probe and directive_queue.depth(session_key) > 0)
        )
    ):
        # The identity gate found nothing to trust. Before treating
        # that as a lost directive, look for the OUT-OF-BAND record:
        # the tool publishes its validated payload straight to the
        # gateway, so on a backend that emits no ``_meta.kiro`` this is
        # the delivery path that still works.
        #
        # The CALL INPUT selects the record; the marker never does.
        # ``_pending_input_digest`` holds the digest of the raw
        # arguments this frame's tool_call carried, the tool parked
        # its validated payload under the same digest of the same
        # arguments, and ``claim`` returns only a record parked under
        # it during THIS turn — then the RECORD's payload is what gets
        # applied. So the two channels must agree: a record aimed at
        # another session (the header is not kernel-attested over TCP,
        # and Windows has no AF_UNIX at all) waits for a call that
        # session's model never makes, and a call no tool validated
        # looks up a record that was never parked. Nothing here reads
        # the result body, so a backend that re-serialises, copies,
        # offloads or caps that body cannot lose the directive. See
        # directive_queue's module docstring.
        #
        # ISOLATION FIRST, though: a NATIVE sub-agent's tool calls
        # surface as flat events on this parent stream (tagged in
        # _native_tc_card) and have no independently bindable slot of
        # their own. On a backend that emits no ``_meta.kiro`` such a
        # frame reaches here with _dir_tool == "", while the record the
        # child's tool parked is keyed by the PARENT's session — so
        # claiming it would apply the child's directive to the parent,
        # the exact mutation the marker path below refuses. Refuse it
        # here too, on the same terms and with the same audit, BEFORE
        # the claim: a claim is destructive (it removes the record), so
        # ordering the check first also keeps a legitimate parent
        # directive from being consumed by a child's frame.
        _in_digest = _pending_input_digest.get(event.tool_call_id, "")
        if event.tool_call_id in _native_tc_card:
            # The identity gate files an identified call under
            # _pending_dir_tool, not _seen_tool_identity, so the
            # directive name for the audit row is _dir_tool.
            _denied_tool = _dir_tool or _seen_tool_identity.get(event.tool_call_id, ("", ""))[1]
            # SETTLED HERE: release the identity mapping so the
            # marker path below does not refuse this same frame a
            # second time (a second denied audit row and a doubled
            # not-applied note).
            _pending_dir_tool.pop(event.tool_call_id, None)
            _dir_tool = ""
            sel().log_tool_invocation(
                session_key=session_key,
                source="mcp-directive",
                tool_name=_denied_tool,
                outcome="denied",
            )
            _out = _redact_tool_field(
                session_directive.strip_marker(_out)
                + (
                    "\n\n[Not applied: a session-bound tool called "
                    "from a sub-agent has no session to act on.]"
                )
            )
            _dir_consumed_out[event.tool_call_id] = _out
            # Refusing to APPLY is not enough on its own: the record
            # the child's tool parked is keyed by the PARENT, so
            # leaving it queued would only defer the mutation to the
            # next frame carrying that input. Retire it — but retire
            # exactly it, by the same input-digest correlation the
            # normal path claims on, so a directive the parent
            # legitimately parked in this same turn survives.
            if _in_digest:
                directive_queue.claim(session_key, _in_digest, not_before=_turn_started)
            _oob = None
        elif _in_digest and any(
            _pending_input_digest.get(_ntc) == _in_digest
            for _ntc in _native_tc_card
            if _ntc != event.tool_call_id
        ):
            # PROVENANCE AMBIGUOUS: a native sub-agent call in this
            # same turn carries the SAME input digest as this parent
            # frame (the same tool called with identical arguments,
            # e.g. two ``resource_status({})`` calls). The
            # record under that digest may be the child's, and the
            # child's own frame will retire it above -- so claiming
            # here could apply a sub-agent's directive to the parent.
            # Refuse rather than guess; the record stays parked for
            # the child frame's isolation path to retire.
            sel().log_tool_invocation(
                session_key=session_key,
                source="mcp-directive",
                tool_name=_seen_tool_identity.get(event.tool_call_id, ("", ""))[1],
                outcome="denied",
            )
            logger.warning(
                "session-directive NOT CLAIMED for %s (tool_call_id=%s): a "
                "native sub-agent call in this turn shares this frame's input "
                "digest, so the parked record's provenance is ambiguous and "
                "the parent must not claim it.",
                session_key,
                event.tool_call_id,
            )
            _oob = None
        else:
            _oob = (
                directive_queue.claim(session_key, _in_digest, not_before=_turn_started)
                if _in_digest
                else None
            )
        if _oob:
            _pending_dir_tool.pop(event.tool_call_id, None)
            _dir_tool = ""
            _applied_kind = str(_oob.get("kind") or "")
            _applied_outcome = await apply_session_directive_outcome(
                state,
                slot,
                session_key,
                _applied_kind,
                dict(_oob.get("args") or {}),
                producer_is_user_facing=_directive_user_origin,
                producer_is_self_wake=_directive_self_wake,
                producer_is_channel=_directive_producer_is_channel(turn),
                producer_wake_loop_id=_directive_loop_id,
                producer_turn_actor=_turn_actor,
            )
            _applied_one = _applied_outcome.text
            _record_terminal_directive(
                turn, _applied_kind, _applied_outcome, event.tool_call_id or ""
            )
            logger.info(
                "session-directive applied OUT OF BAND for %s "
                "(tool_call_id=%s, kind=%s): the marker was unavailable; "
                "the gateway-parked payload selected by "
                "the call's input digest was used.",
                session_key,
                event.tool_call_id,
                _oob.get("kind"),
            )
            # Same surface contract as the marker path: the applier's
            # real outcome replaces the tool's own (deliberately
            # non-committal) text, re-redacted because that string
            # interpolates LLM-derived values.
            _out = _redact_tool_field(session_directive.strip_marker(_out) + "\n\n" + _applied_one)
            _dir_consumed_out[event.tool_call_id] = _out
            _pending_input_digest.pop(event.tool_call_id, None)
            _pending_dir_for_digest.pop(event.tool_call_id, None)
        elif event.tool_call_id not in _dir_consumed_out and session_directive.has_marker(_out):
            # A refusal, so audit it like one. This branch is where a
            # FORGED marker lands — the identity gate recorded nothing
            # and no record was parked — and the sibling refusals
            # (native sub-agent, here and on the marker path) already
            # emit a denied event. Leaving this one to a logger line
            # meant the single most security-relevant outcome of the
            # gate was the only one absent from the SEL trail, so an
            # operator auditing denials saw every case but the attack.
            # The identity mapping is deliberately LEFT in place: for
            # an identified call whose marker did not decode, the
            # marker path below owns the lost-marker notice and the
            # sentinel strip, and it writes no audit row of its own,
            # so this is the frame's only denied event either way.
            sel().log_tool_invocation(
                session_key=session_key,
                source="mcp-directive",
                tool_name=_seen_tool_identity.get(event.tool_call_id, ("", ""))[1],
                outcome="denied",
            )
            logger.warning(
                "session-directive NOT APPLIED: marker present but the "
                "tool call carried no core-MCP identity and no "
                "out-of-band record was parked "
                "(tool_call_id=%s, mcp_server_name=%r, tool_name=%r, "
                "expected mcp_server_name=%r). Either a forged marker, "
                "or this ACP backend emits no _meta.kiro identity AND "
                "could not reach the gateway to park the payload. "
                "CLAIM was attempted for session_key=%r input_digest=%s; "
                "that session's queue currently holds %d parked "
                "record(s) — a non-zero depth here means a record WAS "
                "parked but did not match this frame's call input or "
                "fell outside this turn, while zero means nothing ever "
                "reached /api/session-directive for this key.",
                event.tool_call_id,
                _seen_tool_identity.get(event.tool_call_id, ("", ""))[0],
                _seen_tool_identity.get(event.tool_call_id, ("", ""))[1],
                session_directive.CORE_MCP_SERVER,
                session_key,
                _in_digest[:12] or "none",
                directive_queue.depth(session_key),
            )
            if not _in_digest:
                logger.warning(
                    "session-directive NO CALL INPUT for %s: the frame "
                    "carries the marker sentinel but no tool_call / "
                    "tool_call_update for tool_call_id=%s carried "
                    "rawInput, so no input digest exists to claim on. "
                    "This backend is not emitting the call's arguments.",
                    session_key,
                    event.tool_call_id,
                )
    if not _dir_tool and event.tool_call_id in _dir_consumed_out:
        # A LATER frame for a directive we already consumed: replay
        # the output we produced instead of letting the raw marker
        # text overwrite the applied outcome in the transcript.
        _out = _dir_consumed_out[event.tool_call_id]
    elif _dir_tool:
        if event.tool_call_id in _native_tc_card:
            # SINGLE-CONSUME: one tool call can surface MORE THAN ONE
            # result frame (a mid-stream content frame and the final
            # status=completed rawOutput frame — the same reason the
            # native-card path below keeps _native_result_seen).
            # Without this pop, a directive would be applied twice:
            # two armed loops, two cards, or a repeated mutation.
            _pending_dir_tool.pop(event.tool_call_id, None)
            # Isolation denial — audit it (the one place the gate
            # actively refuses) so it is not a silent drop.
            sel().log_tool_invocation(
                session_key=session_key,
                source="mcp-directive",
                tool_name=_dir_tool,
                outcome="denied",
            )
            # Re-redact: the applier's string interpolates
            # LLM-derived text (autonudge_stop reason, a bad path in
            # set_project's error, exception args), and it OVERWRITES
            # the entry-point _redact_tool_field(_out) above — so it
            # must pass exfil-URL + credential scrubbing before it
            # reaches broadcast_ws / the persisted transcript.
            _out = _redact_tool_field(
                session_directive.strip_marker(_out)
                + (
                    "\n\n[Not applied: a session-bound tool called "
                    "from a sub-agent has no session to act on.]"
                )
            )
            _dir_consumed_out[event.tool_call_id] = _out
        else:
            _dir_args = session_directive.decode(_out, _dir_tool)
            if _dir_args is None and event.tool_final:
                if session_directive.is_refusal(_out):
                    # The tool DECLINED and said so in its own result
                    # text: an oversized payload encode() would not
                    # deliver, a schema rejection ahead of the
                    # handler, or a session this effect can never
                    # apply to. Nothing was applied and the model was
                    # told, so this is the by-design loud failure —
                    # it must not fire the warning below, which
                    # exists to surface a LOST marker.
                    logger.info(
                        "session-directive REFUSED for %r "
                        "(tool_call_id=%s, out_len=%d): the tool "
                        "returned a tagged refusal instead of a "
                        "directive; nothing applied",
                        _dir_tool,
                        event.tool_call_id,
                        len(_out or ""),
                    )
                    # SINGLE-CONSUME + strip: a refusal is terminal,
                    # so release the mapping and cache the
                    # marker-free text for any later frame carrying
                    # this same tool_call_id (which does not
                    # resolve _dir_tool).
                    _pending_dir_tool.pop(event.tool_call_id, None)
                    _out = _redact_tool_field(session_directive.strip_marker(_out))
                    _dir_consumed_out[event.tool_call_id] = _out
                else:
                    # The gate already AUTHENTICATED this as a
                    # directive tool via the canonical _meta
                    # identity, and this is the FINAL frame — so a
                    # marker that does not decode means the effect is
                    # being dropped outright. Never let that be
                    # silent: this exact silence can hide a
                    # rawOutput-envelope escaping bug.
                    # Mid-stream frames legitimately decode to None
                    # and are excluded by the tool_final guard.
                    # TWO causes reach here now that every by-design
                    # decline is tagged: the marker was mangled in
                    # transport, or the tool CRASHED past its own
                    # return (an exception the JSON-RPC layer turned
                    # into text, which never passes
                    # refuse_if_markerless). Name both — a line that
                    # asserts one cause sends an operator hunting an
                    # escaping bug that is not there.
                    logger.warning(
                        "session-directive decode FAILED for %r "
                        "(tool_call_id=%s, out_len=%d) — effect "
                        "dropped. Either the marker was lost in "
                        "transport (a rawOutput-envelope escaping "
                        "regression) or the tool raised past its own "
                        "return, so its decline was never tagged a "
                        "refusal",
                        _dir_tool,
                        event.tool_call_id,
                        len(_out or ""),
                    )
                    append_and_surface(
                        state,
                        slot,
                        "notice",
                        unverified_directive_outcome(_dir_tool),
                        "msg msg-info",
                    )
                    # APPENDED, not substituted: the tool's own text
                    # is the only account of what it did before the
                    # marker was lost, and the agent needs both it
                    # and the recovery instruction.
                    _out = _redact_tool_field(
                        session_directive.strip_marker(_out)
                        + "\n\n"
                        + unverified_directive_notice(_dir_tool)
                    )
                    _pending_dir_tool.pop(event.tool_call_id, None)
                    _dir_consumed_out[event.tool_call_id] = _out
            if _dir_args is not None:
                # SINGLE-CONSUME (see the native branch above): drop
                # the mapping BEFORE applying, so a second result
                # frame for this same tool call cannot re-apply the
                # effect. Left in place when no marker decoded yet —
                # a mid-stream partial frame must not burn the
                # mapping the final frame still needs.
                _pending_dir_tool.pop(event.tool_call_id, None)
                # The tool ALSO parked this payload out of band (one
                # publish per emit, unconditionally — the tool cannot
                # know which consumer will run). Applying both would
                # arm two loops or render two cards, so retire the
                # twin now that the marker path has taken it.
                directive_queue.discard(session_key)
                _applied_outcome = await apply_session_directive_outcome(
                    state,
                    slot,
                    session_key,
                    _dir_tool,
                    _dir_args,
                    producer_is_user_facing=_directive_user_origin,
                    producer_is_self_wake=_directive_self_wake,
                    producer_is_channel=_directive_producer_is_channel(turn),
                    producer_wake_loop_id=_directive_loop_id,
                    producer_turn_actor=_turn_actor,
                )
                _applied_one = _applied_outcome.text
                _record_terminal_directive(
                    turn, _dir_tool, _applied_outcome, event.tool_call_id or ""
                )
                _out = _redact_tool_field(_applied_one)
                _dir_consumed_out[event.tool_call_id] = _out
            else:
                # Recorded directive tool but no valid marker in the
                # result — strip any stray sentinel from the transcript.
                _out = session_directive.strip_marker(_out)
    state.broadcast_ws(
        "tool_result",
        {
            "slot": slot.key,
            "tool_call_id": _tcid,
            "output": _out,
            # False on a mid-stream frame (a shell's streamed stdout
            # arrives as a status-less content update while the
            # command still runs). The crewmate's live status line
            # reads a result as "the call returned, the model is
            # thinking again", so without this the first line of a
            # long build's output flipped it to "Thinking".
            "final": bool(_tool_terminal),
        },
    )
    # If this tool result belongs to a native sub-agent, stream its
    # real output onto that sub-agent's card so the OUTPUT section
    # shows actual results (git log, file contents, summaries) —
    # not just tool names. Mirrors spawn_sub_agents' subagent_chunk.
    _nat_card_r = _native_tc_card.get(event.tool_call_id) if event.tool_call_id else None
    # Redact the FULL output once, then bound each consumer's copy
    # (the native card's 4000 and the PostToolUse hook's 2000):
    # bounding first can cut a credential at the boundary into
    # fragments no redaction regex matches, and sharing the pass
    # keeps the full-text cost single.
    _redacted_out, _ = redact_exfiltration_urls(_out)
    _redacted_out, _ = redact_credentials(_redacted_out)
    if _nat_card_r and event.tool_output and event.tool_call_id not in _native_result_seen:
        _native_result_seen.add(event.tool_call_id)
        _native_card_output_len[_nat_card_r] = _append_native_output(
            _native_card_output.setdefault(_nat_card_r, []),
            f"{_redacted_out[:4000]}\n",
            _native_card_output_len.get(_nat_card_r, 0),
        )
        state.broadcast_ws(
            "subagent_chunk",
            {
                "id": _nat_card_r,
                "slot": slot.key,
                "text": f"{_redacted_out[:4000]}\n",
            },
        )
    # Mark the matching tool message as done so completion state
    # survives page reload (persisted in message meta, replayed via SSE).
    # Also persist the redacted output here so the inline detail panel
    # has data after a chat reload (toolLog Redux state is in-memory).
    # Iterate ALL matching messages — auto-approved tools create two
    # tool entries (🔧 pre-approval + ✅ post-approval) with the same
    # tool_call_id, and both pills should reflect the same output.
    if _tcid:
        for m in slot.messages:
            if m.get("role") == "tool" and m.get("meta", {}).get("tool_call_id") == _tcid:
                _meta = m.setdefault("meta", {})
                _meta["done"] = True
                if (
                    _tcid in _terminal_directive_tcids
                    and _tcid_identifies
                    and str(m.get("ts") or "") not in _rows_before_turn
                    and not _meta.get("ends_turn")
                ):
                    # Structured, never prose: the applied terminal
                    # directive's row is what closes the turn for the
                    # interrupted-turn scan (``is_quiet_end_row``). A
                    # REFUSED call carries the same trusted identity
                    # and no flag, so it reads as an unanswered turn.
                    # Scoped like the app flag above: only a row THIS
                    # turn wrote, and only when the id names one call
                    # -- a transcript-preserving reset can reuse an
                    # earlier turn's tool_call_id, and stamping that
                    # older (refused) row would record it as applied.
                    # Patched live too, or the open client would offer
                    # Resume until its next transcript fetch.
                    _meta["ends_turn"] = True
                    _ends_ts = str(m.get("ts") or "")
                    if _ends_ts:
                        try:
                            state.broadcast_ws(
                                "chat_message_update",
                                {
                                    "slot": slot.key,
                                    "ts": _ends_ts,
                                    "meta": {"ends_turn": True},
                                },
                            )
                        except Exception:
                            logger.debug("ends_turn flag broadcast failed", exc_info=True)
                # Written only when this row HAD an app record, and
                # never written False: absent means "no app", which
                # is also what every row predating this field says.
                # The frontend renders its there-is-an-app-here
                # notice from this alone, so a flag on a row that
                # never had an app would point at nothing.
                # `_app_on_row` is THIS frame's: it is assigned
                # unconditionally earlier in this same
                # _on_tool_result call, which is what keeps one
                # tool call's app off another call's row.
                #
                # Gated on the row being one THIS TURN appended, for
                # the same reason the claim is: this walk selects by
                # `tool_call_id`, and a transcript-preserving reset
                # can leave a historical row holding the id the
                # backend has just reissued. Without the gate that
                # old row is flagged too -- permanently, since the
                # flag is never written False below -- and because
                # the client draws the notice on the FIRST flagged
                # row for the id, the stale row would answer for the
                # app while the row that actually has it is passed
                # over. `done` and `output` keep the id-wide walk
                # they have always had: both describe the CALL, and
                # a row replaying that call is not made wrong by
                # them, where a claim of "an app was here" is.
                #
                # Gated on the id identifying one call for the same
                # reason: credential redaction collapses the whole
                # field to one constant, so without this a second
                # call's row in the same turn takes a permanent flag
                # for an app it never had.
                if (
                    _app_on_row
                    and _tcid_identifies
                    and str(m.get("ts") or "") not in _rows_before_turn
                ):
                    # ONE update rather than two assignments. The periodic
                    # flush takes `slot.messages` as a shallow copy of
                    # REFERENCES and serializes these same live dicts on a
                    # worker thread, so a snapshot landing between two
                    # assignments stores `mcp_app` with no `mcp_app_lead`.
                    # The client draws the notice on the lead alone, so that
                    # row renders nothing, and the restore pass cannot
                    # repair it: it reads a surviving `mcp_app` as proof the
                    # lead survived, marks the claim's lead taken and skips,
                    # and the claim is swept at its TTL. `dict.update`
                    # applies both keys in one step, so a reader holding the
                    # interpreter sees both or neither.
                    _row_flags: dict[str, Any] = {"mcp_app": True}
                    # The FIRST row this occurrence flags is its lead,
                    # and the client draws the notice on that row
                    # alone. Stamped here because only this loop knows
                    # which rows belong to THIS call: the client sees
                    # a flat transcript where a pre-reset row that had
                    # its own app keeps `mcp_app` forever, so any
                    # client-side rule that picked the earliest
                    # flagged row for an id chose that stale row and
                    # suppressed the notice for the app this turn
                    # really lost. `slot.messages` is in transcript
                    # order, so the first row this branch reaches is
                    # the earliest of the occurrence, and the marker
                    # needs no ordering comparison to read.
                    if not _app_flag_rows:
                        _row_flags["mcp_app_lead"] = True
                    _meta.update(_row_flags)
                    _app_flag_rows.append(str(m.get("ts") or ""))
                # A terminal frame carrying no renderable output states a
                # STATUS, not an empty output, so it must not overwrite an
                # output an earlier frame for this same call already
                # persisted -- the panel would then show nothing for a tool
                # whose output this process did see. Absent stays absent, so
                # a first terminal frame with no output still reads as one.
                if _out or "output" not in _meta:
                    _meta["output"] = _out
        # Tell open clients about the app flag as well as storing it.
        # `chat.mcpApps` is a BOUNDED cache, so a session that opens
        # many apps evicts the oldest payload while its row is still
        # on screen; without this the row goes blank there and only
        # names the app after a reload. The reducer MERGES meta, so
        # the patch carries that one key and nothing else -- notably
        # not the row's output, which is capped at 1 MB and already
        # delivered.
        #
        # One patch per flagged row, addressed by that row's own
        # `ts`, because a tool_call_id names TWO rows for an
        # auto-approved call and the reducer would patch only the
        # newest -- so both rows are correct live, as well as after a
        # reload from the persisted write above. A `ts` is the right
        # address for THIS send and not a durable identity: it is
        # resolved against the transcript the sender is holding, in
        # the same turn that wrote these rows. The claim, which is
        # read back on a later process, records `meta.mid` instead.
        for _i, _flag_ts in enumerate(_app_flag_rows):
            if not _flag_ts:
                continue
            # The lead marker travels with the flag, or an open client
            # would hold a flagged row that the stored transcript calls
            # the lead and the live view calls neither -- the notice
            # would then appear only after a reload.
            _patch_meta: dict[str, Any] = {"mcp_app": True}
            if _i == 0:
                _patch_meta["mcp_app_lead"] = True
            try:
                state.broadcast_ws(
                    "chat_message_update",
                    {
                        "slot": slot.key,
                        "ts": _flag_ts,
                        "meta": _patch_meta,
                    },
                )
            except Exception:
                # The stored rows are already correct; a client
                # reconciles from slot detail on its next fetch.
                logger.debug("mcp-app flag broadcast failed", exc_info=True)
    # Fire PostToolUse hooks
    _tool_name = _pending_tools.pop(event.tool_call_id, "")
    try:
        _redacted_out_bounded = _redacted_out[:2000]
        await _fire(
            turn,
            HOOK_EVENT_POST_TOOL_USE,
            tool_name=_tool_name,
            tool_response={"output": _redacted_out_bounded},
        )
    except Exception:
        logger.debug("PostToolUse hook error", exc_info=True)
    if (
        _tool_terminal
        and not _compact_after_turn_noticed
        and not is_slash
        and not turn.assistant_text
        and not _deferred_cut_text
    ):
        # The context crossed the threshold while this turn is still
        # working. Compaction waits for the turn to end, so say so to
        # the person, once per turn.
        _compact_after_turn_noticed = await _notice_compaction_after_turn(
            state, slot, client, session_key
        )
        turn._compact_after_turn_noticed = _compact_after_turn_noticed
