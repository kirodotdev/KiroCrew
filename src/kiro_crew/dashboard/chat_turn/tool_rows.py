"""What a dashboard turn's tool rows persist and broadcast: row meta, the WS payload, the
refinement rewrite, the tool-call identity key and the rows-only save, and the stream's
tool-call and tool-update arms (``_on_tool_call``, ``_on_tool_call_update``)."""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_utils import (
    _tool_identity_fields,
    open_construct_at_end,
    restore_replacement_if_handover_did_not_land,
    slot_history_key,
    tighten_replacement_to_restricted_original,
)
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.tool_call_title import derive_tool_call_title
from kiro_crew.wakatime.heartbeats import is_coding_event

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _MAX_TOOL_PURPOSE,
        LLMEvent,
        _git_repo_touches,
        _redact_tool_field,
        capabilities_of,
        crew_log_emit,
        fire_tool_hooks,
        logger,
        redact_credentials,
        redact_exfiltration_urls,
        save_slot_off_loop,
        sel,
        session_directive,
    )
    from kiro_crew.dashboard.chat_turn.decisions_hooks import _tool_risk_meta
    from kiro_crew.dashboard.chat_turn.deny_chokepoints import _redact_display_text
    from kiro_crew.dashboard.chat_turn.file_changes import (
        _failed_write_entries,
        _record_turn_snapshot,
        _snapshot_write_target,
    )
    from kiro_crew.dashboard.chat_turn.native_cards import (
        _append_native_output,
        _post_native_question_card,
    )
    from kiro_crew.dashboard.chat_turn.recipient import (
        _retire_mirror_thinking,
        cross_surface_withheld,
        slack_publication_withheld,
    )
    from kiro_crew.dashboard.chat_turn.turn_ledgers import _report_consumed
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn
    from kiro_crew.dashboard.chat_turn.turn_reply_seams import _cut_segment


async def _persist_tool_result_rows(state: Any, slot: Any) -> Any:
    """Persist tool rows without leaving a same-file replacement looser."""
    history_key = slot_history_key(slot)
    try:
        tightening = tighten_replacement_to_restricted_original(state, slot.key, slot)
    except UnknownMemoryStore:
        tightening = None
        logger.warning(
            "Slot %s: replacement rebound twice during tightening; writing the tail "
            "under the ratcheted line without tightening the live replacement",
            slot.key,
        )
    try:
        committed = await save_slot_off_loop(state, slot, rows_only=True)
    except Exception:
        await restore_replacement_if_handover_did_not_land(state, slot.key, tightening, history_key)
        raise
    if not committed:
        await restore_replacement_if_handover_did_not_land(state, slot.key, tightening, history_key)
    return committed


def _refined_tool_row_content(existing: str, new_title: str) -> str | None:
    """The rewritten content for a tool row a ``tool_call_update`` refines, or None.

    kiro-cli sends the resolved title after a permission is answered, and the row
    is re-rendered as ``"<icon> <title>"``, preserving whichever leading icon the
    row already carries (🔧 running / ✅ done / 🚫 refused).

    Returns None for a REFUSAL row, which must be left alone: it is terminal (the
    tool will not run, so a better title adds nothing) and its content is
    ``"🚫 <title> — <reason>"``, so re-rendering from the title alone deletes the
    reason. That tail is the user's only visible explanation, and the model has
    already been told the reason in-band — dropping it leaves the human looking at
    a blocked row with no cause while the agent acts on one they cannot see.
    """
    prefix = existing[:1] if existing[:1] in ("🔧", "✅", "🚫") else "🔧"
    if prefix == "🚫":
        return None
    return f"{prefix} {new_title}"


# Distinct redacted tool_call_ids one turn tracks a source digest for. The ids
# come from the LLM, so their number is not the runner's to trust; past this a
# further id is treated as collapsed, which withholds its app notice rather than
# letting the turn's state grow with the stream. Far above any real turn's tool
# count, so a genuine conversation never reaches it.
_MAX_TCID_SOURCES = 512


# Bytes of a redacted tool_call_id this turn will track an identity for. The ids
# come from the LLM, so neither their number nor their LENGTH is the runner's to
# trust, and a bound on the count alone still retains 512 unbounded strings. Well
# under `_MAX_TOOL_FIELD`, so a tracked id is never one `_redact_tool_field`
# truncates -- two different over-long ids share a truncated prefix, so a
# truncated id cannot be told from another and must not be tracked at all.
_MAX_TCID_LEN = 1024


def _tcid_identity_key(tcid: str | None) -> str:
    """Bounded key for a redacted ``tool_call_id``, or ``""`` when it names nothing.

    The key is a digest, so what the turn retains is 16 characters however long the
    id was. It is taken over ``_redact_tool_field``'s output -- the same call the
    reader makes -- because that, not the raw field, is the value rows are stored
    and compared under, and deriving the key with the reader's own function is what
    makes the two ends agree by construction rather than by assumption.

    An absent or over-long id returns ``""``, which no caller treats as identifying,
    so both fail towards withholding the notice.
    """
    if not tcid:
        return ""
    if len(tcid.encode("utf-8", "replace")) > _MAX_TCID_LEN:
        return ""
    canonical = _redact_tool_field(tcid)
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:16]


def _tool_meta(event: "LLMEvent") -> dict[str, str] | None:
    """Build the meta dict persisted on a tool message — `tool_call_id`,
    `purpose`, and the full redacted `input`. Output is appended later by the
    EVENT_TOOL_RESULT handler. The inline detail panel is the only source of
    truth for what an agent ran, so the meta carries the full content (capped
    at 1 MB and 8 KB respectively as defensive safety nets).

    `tool_call_id` is redacted to match `_broadcast_auto_tool` in
    `chat_utils.py`, which has always redacted before the WS broadcast.
    Keeping it consistent across persisted meta and live broadcast means the
    frontend join (`toolLog[i].tool_call_id` ↔ `message.meta.tool_call_id`)
    works whether the entry came from the live tool-call event or from a
    historical replay. Comparison sites (e.g. EVENT_TOOL_RESULT) must
    redact `event.tool_call_id` before matching against the stored value."""
    if not event.tool_call_id:
        return None
    return {
        "tool_call_id": _redact_tool_field(event.tool_call_id),
        "purpose": _redact_tool_field(event.tool_purpose, limit=_MAX_TOOL_PURPOSE),
        "input": _redact_tool_field(event.tool_input),
        # ACP tool kind (read/edit/execute/…). The dashboard gates the inline
        # diff-card promotion on kind == "edit" so a shell command whose input
        # happens to look like a diff is never promoted; persisting it keeps
        # historical rows gate-able identically to live ones.
        "kind": _redact_tool_field(event.tool_kind, limit=64),
        # Trusted tool identity from ``_meta.kiro``, present only when the backend
        # sent one (``_tool_identity_fields`` omits rather than writes empty). The
        # dashboard's title derivation reads these for MCP calls so a replayed row
        # does not have to parse ``@server/tool`` out of the title; both are
        # optional there, so a row without them still renders.
        **_tool_identity_fields(event),
    }


def _tool_call_ws_payload(event: "LLMEvent") -> dict[str, str | bool]:
    """Build the live dashboard payload for a tool invocation.

    ``is_shell`` is intentionally an explicit capability signal rather than a
    frontend guess based on the tool title. Shell commands usually have no
    trustworthy total, so the dashboard can render an indeterminate status
    today while future tools can add a real progress mode without changing the
    tool-card data flow.
    """
    title, _ = redact_exfiltration_urls(event.title)
    title, _ = redact_credentials(title)
    kind, _ = redact_exfiltration_urls(event.tool_kind)
    kind, _ = redact_credentials(kind)
    return {
        "slot": "",  # Filled by the caller because it belongs to the session.
        "tool": title,
        "kind": kind,
        "is_shell": event.is_shell,
        "tool_call_id": _redact_tool_field(event.tool_call_id),
        "purpose": _redact_tool_field(event.tool_purpose, limit=_MAX_TOOL_PURPOSE),
        "input_preview": _redact_tool_field(event.tool_input),
        **_tool_identity_fields(event),
    }


def _mirror_task_label(event: Any) -> str:
    """The Slack mirror's task-step label for a tool call with no purpose line.

    The same derivation the dashboard transcript's rows and the channel task
    labels render (``tool_call_title``, the Python half of
    ``toolCallTitle.ts``), so a step reads "Git status" or "Wait" rather than
    the raw ``@server/tool`` id. Falls back to the transport title only when
    the derivation has nothing at all.
    """
    derived = derive_tool_call_title(
        title=event.title or "",
        kind=event.tool_kind or "",
        raw_input=event.tool_input or "",
        is_shell=bool(event.is_shell),
        tool_name=event.tool_name or "",
        mcp_server=event.mcp_server_name or "",
    )
    return derived.title or event.title or ""


async def _on_tool_call(turn: _ChatTurn, event) -> None:
    """A tool call the stream announces: count it, row it and open its card."""
    _activity_after_terminal = turn._activity_after_terminal
    _crew_log_sid = turn._crew_log_sid
    _crew_log_turn_no = turn._crew_log_turn_no
    _deferred_cut_text = turn._deferred_cut_text
    _mirror_active_task = turn._mirror_active_task
    _mirror_active_task_title = turn._mirror_active_task_title
    _mirror_chan = turn._mirror_chan
    _mirror_stream_ts = turn._mirror_stream_ts
    _mirror_task_counter = turn._mirror_task_counter
    _mirror_thread = turn._mirror_thread
    _native_card_output = turn._native_card_output
    _native_card_output_len = turn._native_card_output_len
    _native_tc_card = turn._native_tc_card
    _pending_dir_for_digest = turn._pending_dir_for_digest
    _pending_dir_tool = turn._pending_dir_tool
    _pending_input_digest = turn._pending_input_digest
    _pending_tools = turn._pending_tools
    _repeat_loop = turn._repeat_loop
    _seen_tool_identity = turn._seen_tool_identity
    _shell_tool_calls = turn._shell_tool_calls
    _tcid_collapsed = turn._tcid_collapsed
    _tcid_first_source = turn._tcid_first_source
    _turn_flushed_visible_text = turn._turn_flushed_visible_text
    _turn_tool_calls = turn._turn_tool_calls
    _turn_tool_identities = turn._turn_tool_identities
    _wt_pending_coding = turn._wt_pending_coding
    _wt_pending_dropped = turn._wt_pending_dropped
    client = turn.client
    in_tool_group = turn.in_tool_group
    message = turn.message
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    _turn_tool_calls += 1
    turn._turn_tool_calls = _turn_tool_calls
    _repeat_loop.note_call(event.tool_call_id, event.tool_name or "", event.tool_input, event.title)
    if turn._terminal_directive_applied:
        _activity_after_terminal += 1
        turn._activity_after_terminal = _activity_after_terminal
    if event.is_shell and event.tool_call_id and len(_shell_tool_calls) < _MAX_TCID_SOURCES:
        _shell_tool_calls.add(event.tool_call_id)
    if event.tool_call_id:
        if is_coding_event(
            event.tool_name or "",
            event.tool_kind or "",
            bool(event.is_shell),
            event.mcp_server_name or "",
        ):
            # Retain only ids that the turn's shared identity tracker
            # still proves name one call. Its first-source map is
            # already capped at `_MAX_TCID_SOURCES`, so this set stays
            # within the same bound without an independent admission
            # cap. A key omitted by that cap is tallied; a collapsed
            # key is withheld as ambiguous rather than misreported as
            # a cap drop.
            _wt_key = _tcid_identity_key(event.tool_call_id)
            if _wt_key and _wt_key in _tcid_first_source and _wt_key not in _tcid_collapsed:
                _wt_pending_coding.add(_wt_key)
            elif _wt_key and _wt_key not in _tcid_first_source:
                _wt_pending_dropped += 1
                turn._wt_pending_dropped = _wt_pending_dropped
    _turn_tool_identities.append(
        (
            event.tool_call_id or "",
            event.mcp_server_name or "",
            event.tool_name or "",
            event.tool_identity_trusted is True,
        )
    )
    # Flush pre-tool text silently (no broadcast) so it persists,
    # but keep the streaming message in place for correct tool ordering.
    # The call does not end the reply, so the run the wire redactor
    # still withholds waits for the text after it (_cut_segment).
    if not in_tool_group and turn.assistant_text:
        _open_construct = open_construct_at_end(turn.assistant_text)
        if _open_construct:
            _deferred_cut_text = turn.assistant_text
            turn._deferred_cut_text = _deferred_cut_text
            logger.debug(
                "segment cut deferred on %s — %s still open",
                slot.key,
                _open_construct,
            )
        else:
            _cut_segment(turn, broadcast=False)
            _turn_flushed_visible_text = True
            turn._turn_flushed_visible_text = _turn_flushed_visible_text
    # AFTER the flush, because seq is the order a reader folds on and
    # the model narrating before it calls a tool is the common case:
    # `_flush_segment` is what appends this turn's `message/sent`, so
    # emitting the call first put the tool ahead of the text that
    # preceded it and a fold would read the narration as the call's
    # result.
    crew_log_emit.on_tool_called(
        _crew_log_sid,
        _crew_log_turn_no,
        name=event.tool_name or "",
        server=event.mcp_server_name or "",
        kind=event.tool_kind or "",
        call_id=event.tool_call_id or "",
        # Digested to a hash and a size, never recorded: that answers
        # "same arguments as last time" and "how big was it" without
        # the crew log becoming where a shell command's secrets land.
        # This is the already-redacted display string, so a value the
        # user could not see is not hashed into a durable file either.
        args=event.tool_input or "",
    )
    in_tool_group = True
    turn.in_tool_group = in_tool_group
    _turn_emitted = True  # tool side effect — transient retry now unsafe
    turn._turn_emitted = _turn_emitted
    await _report_consumed(turn, irreversible=True)
    # Broadcast for real-time visibility and persist
    _tool_payload = _tool_call_ws_payload(event)
    _tool_payload["slot"] = slot.key
    # Snapshot file BEFORE write tools execute. Accumulates per-turn,
    # flushed to assistant message meta in _flush_file_changes on turn end.
    # Prefer the in-band diff_old_text from the ACP content block
    # (authoritative) over a disk read which races with the write.
    # Offloaded: strReplace reconstruction reads the file from
    # disk, and a slow/hung filesystem must not stall the loop.
    _file_snapshot = await asyncio.to_thread(
        _snapshot_write_target,
        event.raw_tool_params,
        diff_old_text=event.diff_old_text,
        diff_path=event.diff_path,
    )
    if _file_snapshot:
        _record_turn_snapshot(
            slot,
            _file_snapshot,
            _tcid_identity_key(getattr(event, "tool_call_id", None)),
        )
    state.broadcast_ws(
        "tool_call",
        _tool_payload,
    )
    # A proposed call is not an executed touch. Snapshot only raw
    # strings here; the terminal result releases them to the walker.
    _git_repo_touches.note_tool_call(
        slot,
        event.raw_tool_params,
        tool_call_id=event.tool_call_id or "",
        background_tasks=getattr(state, "_background_tasks", None),
        tool_kind=event.tool_kind or "",
        diff_path=event.diff_path or "",
        is_shell=bool(event.is_shell),
    )
    # AFTER the live ``tool_call`` broadcast above and BEFORE the
    # row is appended: the pill the open tab draws is not delayed by
    # the annotation, and the record reaches both doors -- the
    # ``chat_message`` frame ``append`` broadcasts from inside the
    # call, and the persisted transcript line -- from one write.
    # Returns None for every session this seam is off or unsampled
    # for, which is every session by default.
    _tool_row_meta = _tool_meta(event)
    _risk_meta = await _tool_risk_meta(
        state,
        slot,
        event,
        session_key=session_key,
        message=message,
        calls_this_turn=_turn_tool_calls,
    )
    if _risk_meta:
        _tool_row_meta = {**(_tool_row_meta or {}), **_risk_meta}
    slot.append("tool", f"🔧 {_tool_payload['tool']}", "msg msg-tool", meta=_tool_row_meta)
    sel().log_tool_invocation(
        session_key=session_key,
        agent=slot.agent or "kirocrew",
        source="dashboard",
        tool_name=_redact_display_text(event.title),
        tool_kind=event.tool_kind,
        outcome="invoked",
    )
    # AskUserQuestion: validate, then post through the server-side
    # owner so the card carries a ``card_id`` and retires with the
    # next user row / consumed steer like every other question card.
    if event.title == "AskUserQuestion" and event.tool_input:
        await _post_native_question_card(state, slot.key, event.tool_input)
    # Fire PreToolUse hooks for auto-approved tools.
    # NOTE: For EVENT_TOOL_CALL, hooks are informational only - the tool
    # is already running (auto-approved by kiro-cli). Hook results cannot
    # block execution. Hook scripts can log, audit, or trigger side effects.
    _raw = event.title or ""
    if _raw.startswith("Running: "):
        _raw = _raw[9:]
    if event.tool_call_id:
        _pending_tools[event.tool_call_id] = _raw
        # The claim key binds the TOOL to its arguments, so only a call
        # that resolves to a directive tool records one: trusted
        # _meta.kiro identity first, else kiro-agent's own
        # ``@kirocrew-core/<tool>`` WIRE title -- never event.title,
        # which select_tool_title fills from a shell call's
        # model-authored rawInput.description. A shell call, or any
        # other tool, records nothing and can claim nothing.
        _dir_for_digest = session_directive.directive_tool_from_call(
            event.mcp_server_name or "", event.tool_name or "", event.wire_title or ""
        )
        if _dir_for_digest:
            _pending_dir_for_digest[event.tool_call_id] = _dir_for_digest
            if event.raw_tool_params is not None:
                _pending_input_digest[event.tool_call_id] = session_directive.event_input_digest(
                    _dir_for_digest,
                    event.raw_tool_params,
                    event.mcp_server_name,
                    event.tool_name,
                )
        # Forgery gate: record the directive-tool name ONLY
        # from the trusted _meta.kiro identity — never the title.
        # The single shared predicate (also used by the messaging
        # TurnDriver) requires Kiro Crew's OWN core MCP server and a
        # canonical directive-tool name; a shell tool (no
        # mcp_server_name, canonical tool_name "execute_bash") or a
        # third-party server exposing a same-named tool can never
        # register here. Recorded at EVENT_TOOL_CALL only (the
        # UPDATE refinement rewrites titles).
        _cannon = session_directive.directive_tool_for(event.mcp_server_name, event.tool_name)
        if _cannon:
            _pending_dir_tool[event.tool_call_id] = _cannon
        else:
            _seen_tool_identity[event.tool_call_id] = (
                event.mcp_server_name or "",
                event.tool_name or "",
            )
        from kiro_crew import undelivered_call_watch

        if turn._undelivered_watch is None:
            turn._undelivered_watch = undelivered_call_watch.UndeliveredCallWatch(
                session_key or "", turn.client
            )
        turn._undelivered_watch.on_call(event)
    # If this tool call belongs to a native sub-agent (mapped via
    # _kiro.dev/session/update), stream it onto that sub-agent's card.
    _nat_card = _native_tc_card.get(event.tool_call_id) if event.tool_call_id else None
    if _nat_card:
        _ntool, _ = redact_exfiltration_urls(_raw or event.title or "")
        _ntool, _ = redact_credentials(_ntool)
        _ntool = _ntool[:80]
        _native_card_output_len[_nat_card] = _append_native_output(
            _native_card_output.setdefault(_nat_card, []),
            f"\u2192 {_ntool}\n",
            _native_card_output_len.get(_nat_card, 0),
        )
        state.broadcast_ws(
            "subagent_chunk",
            {"id": _nat_card, "slot": slot.key, "text": f"\u2192 {_ntool}\n"},
        )
    # Where Crew fires the spec's hooks, every call a PreToolUse hook
    # covers comes back as a permission request (the projection
    # withholds its auto-approval) and KAS sends this frame first, so
    # the permission request is where the hooks run; firing here too
    # would run each twice.
    if not capabilities_of(client).crew_fires_spec_hooks:
        await fire_tool_hooks(state._hook_store, event.title, event.tool_input)
    # Mirror tool call to linked Slack stream. Fenced like the reply
    # legs: a tool's purpose line is the peer's steer showing through in
    # what the model chose to do next, published to a thread whose owner
    # is resolved live. The check is a dict emptiness test on the
    # overwhelming majority of turns -- `cross_surface_withheld` returns
    # before probing anything when no peer steer is recorded -- so paying
    # it per event costs nothing on a turn nobody interfered with. The
    # stream's destination is the thread cached at turn start, judged
    # as the room it is beside the live comparison.
    if (
        _mirror_chan
        and _mirror_thread
        and state.slack_client
        and not cross_surface_withheld(state, slot)
        and not slack_publication_withheld(state, slot, _mirror_chan, _mirror_thread)
    ):
        try:
            if not turn._mirror_stream_tried:
                # First mirrored tool: the task stream takes over from
                # the "Thinking…" placeholder, whose line would
                # otherwise sit above the plan's own header for good.
                turn._mirror_stream_tried = True
                _mirror_stream_ts = (
                    await state.slack_client.start_stream(_mirror_chan, _mirror_thread) or ""
                )
                turn._mirror_stream_ts = _mirror_stream_ts
                if _mirror_stream_ts:
                    turn._mirror_thinking_ts = await _retire_mirror_thinking(
                        state.slack_client, _mirror_chan, turn._mirror_thinking_ts
                    )
        except Exception:
            logger.debug("Mirror task stream start failed", exc_info=True)
        if _mirror_stream_ts:
            try:
                if _mirror_active_task:
                    await state.slack_client.append_task(
                        _mirror_chan,
                        _mirror_stream_ts,
                        _mirror_active_task,
                        _mirror_active_task_title,
                        "complete",
                    )
                _mirror_task_counter += 1
                turn._mirror_task_counter = _mirror_task_counter
                _mirror_active_task = f"tool_{_mirror_task_counter}"
                turn._mirror_active_task = _mirror_active_task
                _task_title = event.tool_purpose or _mirror_task_label(event)
                _task_title, _ = redact_exfiltration_urls(_task_title)
                _task_title, _ = redact_credentials(_task_title)
                _task_title = _task_title[:75]
                _mirror_active_task_title = _task_title
                turn._mirror_active_task_title = _mirror_active_task_title
                await state.slack_client.append_task(
                    _mirror_chan,
                    _mirror_stream_ts,
                    _mirror_active_task,
                    _task_title,
                    "in_progress",
                )
            except Exception:
                logger.debug("Mirror tool task failed", exc_info=True)


async def _on_tool_call_update(turn: _ChatTurn, event) -> None:
    """An update to a tool call already on the turn."""
    _failed_write_calls = turn._failed_write_calls
    _pending_dir_for_digest = turn._pending_dir_for_digest
    _pending_input_digest = turn._pending_input_digest
    _pending_tools = turn._pending_tools
    _repeat_loop = turn._repeat_loop
    _shell_tool_calls = turn._shell_tool_calls
    _tcid_collapsed = turn._tcid_collapsed
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    if event.is_shell and event.tool_call_id and len(_shell_tool_calls) < _MAX_TCID_SOURCES:
        _shell_tool_calls.add(event.tool_call_id)
    # claude-agent-acp emits an initial `tool_call` with empty
    # input (title falls back to generic name like "Terminal" or
    # "grep") followed by a `tool_call_update` carrying the
    # populated rawInput and a refined title from the upstream
    # `toolInfoFromToolUse`.  Patch the existing pill (toolLog)
    # and the persisted message in place by tool_call_id so the
    # user sees the actual command rather than the stub.
    if not event.tool_call_id:
        return None
    if event.tool_input:
        _repeat_loop.note_call(
            event.tool_call_id, event.tool_name or "", event.tool_input, event.title
        )
    _dir_refresh = _pending_dir_for_digest.get(event.tool_call_id, "")
    if not _dir_refresh:
        # claude-agent-acp's initial tool_call carries a generic title
        # and the refinement carries the real one, so the tool may only
        # resolve here. The gate is the same resolver either way.
        _dir_refresh = session_directive.directive_tool_from_call(
            event.mcp_server_name or "", event.tool_name or "", event.wire_title or ""
        )
        if _dir_refresh:
            _pending_dir_for_digest[event.tool_call_id] = _dir_refresh
    if _dir_refresh and event.raw_tool_params is not None:
        # The refinement carries the COMPLETE params; the initial
        # tool_call may have streamed none. Same digest the tool took.
        _pending_input_digest[event.tool_call_id] = session_directive.event_input_digest(
            _dir_refresh,
            event.raw_tool_params,
            event.mcp_server_name,
            event.tool_name,
        )
    try:
        _tcid_upd = _redact_tool_field(event.tool_call_id)
        _title_upd = ""
        if event.title:
            _title_upd, _ = redact_exfiltration_urls(event.title)
            _title_upd, _ = redact_credentials(_title_upd)
        _kind_upd = ""
        if event.tool_kind:
            _kind_upd, _ = redact_exfiltration_urls(event.tool_kind)
            _kind_upd, _ = redact_credentials(_kind_upd)
        _input_upd = _redact_tool_field(event.tool_input) if event.tool_input else ""
        _purpose_upd = (
            _redact_tool_field(event.tool_purpose, limit=_MAX_TOOL_PURPOSE)
            if event.tool_purpose
            else ""
        )
        # Snapshot file BEFORE the write tool actually executes.
        # Initial tool_call had empty rawInput so no snapshot was
        # taken there; this is the first event with the file path.
        # Prefer the in-band diff_old_text from the ACP content
        # block (authoritative) over a disk read which races with
        # the write. Offloaded: reconstruction reads from disk and
        # a slow/hung filesystem must not stall the loop.
        _file_snapshot_upd = await asyncio.to_thread(
            _snapshot_write_target,
            event.raw_tool_params,
            diff_old_text=event.diff_old_text,
            diff_path=event.diff_path,
        )
        if _file_snapshot_upd:
            _record_turn_snapshot(
                slot,
                _file_snapshot_upd,
                _tcid_identity_key(getattr(event, "tool_call_id", None)),
            )
            # A refinement can follow its failed result, even as the
            # call's first snapshot: claim no diff, while the id still
            # names one call.
            _refined_writer = _tcid_identity_key(event.tool_call_id)
            if _refined_writer in _failed_write_calls and _refined_writer not in _tcid_collapsed:
                for _held in _failed_write_entries(slot, _refined_writer):
                    _held["unverified_write"] = True
        # Refresh the toolLog entry (sseToolActivity merges by id).
        state.broadcast_ws(
            "tool_call",
            {
                "slot": slot.key,
                "tool": _title_upd,
                "kind": _kind_upd,
                "tool_call_id": _tcid_upd,
                "input_preview": _input_upd,
                # The update is the event that supplies the real
                # shell title/input, so it must carry the same
                # capability signal as the initial tool_call.
                "is_shell": event.is_shell,
                "is_update": True,
                # Omitted rather than sent empty: consumers merge a
                # refinement field-by-field and read an absent
                # `purpose` as "keep what the initial tool_call
                # supplied", so an empty value would blank a good
                # purpose (the session list's running-status line).
                **({"purpose": _purpose_upd} if _purpose_upd else {}),
                **_tool_identity_fields(event),
            },
        )
        # The refinement may supply the first path (Claude streams
        # empty input first). Retain it until successful completion.
        _git_repo_touches.note_tool_call(
            slot,
            event.raw_tool_params,
            tool_call_id=event.tool_call_id,
            background_tasks=getattr(state, "_background_tasks", None),
            tool_kind=event.tool_kind or "",
            diff_path=event.diff_path or "",
            is_shell=bool(event.is_shell),
        )
        # Update the audit log so the SEL trail captures the
        # refined title/kind, not just the "Terminal"/"grep" stub
        # logged at the initial EVENT_TOOL_CALL.
        sel().log_tool_invocation(
            session_key=session_key,
            agent=slot.agent or "kirocrew",
            source="dashboard",
            tool_name=_title_upd,
            tool_kind=_kind_upd,
            outcome="refined",
        )
        # Patch the persisted tool message in place so its content
        # shows the refined title and meta carries the populated
        # input.  Walk in reverse and break on the first match —
        # auto-approved tools may have a later "✅ {title}" entry
        # with the same tool_call_id, and we don't want to
        # overwrite that post-approval marker. Preserve whatever
        # leading icon (🔧/✅/🚫) the existing message has.
        _meta_patch: dict[str, str] = {}
        if _input_upd:
            _meta_patch["input"] = _input_upd
        # A refinement is the only event carrying the purpose when the
        # initial tool_call streamed an empty rawInput, so the patch has
        # to reach the PERSISTED meta too: _tool_meta() wrote "" there,
        # and the reloaded transcript reads meta.purpose (ToolCallLine),
        # so a live-only fix would lose the purpose on the next reload.
        if _purpose_upd:
            _meta_patch["purpose"] = _purpose_upd
        _patched = False
        _patched_content: str | None = None
        for m in reversed(slot.messages):
            if m.get("role") != "tool":
                continue
            if m.get("meta", {}).get("tool_call_id") != _tcid_upd:
                continue
            if _title_upd:
                _refined = _refined_tool_row_content(m.get("content", "") or "", _title_upd)
                # None == a refusal row: keep its reason (see the
                # helper). Meta patches below still apply; only the
                # content rewrite is skipped.
                if _refined is not None:
                    _patched_content = _refined
                    m["content"] = _patched_content
                    slot.invalidate_source_links()
            if _meta_patch:
                m_meta = m.setdefault("meta", {})
                m_meta.update(_meta_patch)
            _patched = True
            break
        if _patched:
            slot._dirty = True
            state.broadcast_ws(
                "chat_message_update",
                {
                    "slot": slot.key,
                    "tool_call_id": _tcid_upd,
                    **({"content": _patched_content} if _patched_content else {}),
                    **({"meta": _meta_patch} if _meta_patch else {}),
                },
            )
        # Update _pending_tools so PostToolUse hooks see the
        # refined name (e.g. "ls /tmp") instead of the stub
        # ("Terminal").  Strip the "Running: " prefix to match
        # the EVENT_TOOL_CALL handler's normalization — hooks
        # match by tool name and would miss otherwise.
        if event.title and event.tool_call_id in _pending_tools:
            _refined_name = event.title
            if _refined_name.startswith("Running: "):
                _refined_name = _refined_name[9:]
            _pending_tools[event.tool_call_id] = _refined_name
    except Exception:
        logger.warning(
            "EVENT_TOOL_CALL_UPDATE handler failed for tool_call_id=%s",
            event.tool_call_id,
            exc_info=True,
        )
