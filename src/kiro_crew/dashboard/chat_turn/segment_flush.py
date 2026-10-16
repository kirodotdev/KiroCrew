"""Finalizing a dashboard turn's assistant text segment: redaction with its records, row meta,
the variant stash, the [OPTIONS:] footer audit and the widget registration; a local command
reply is persisted through the same redaction."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        MAX_BLOCKED_LINKS_PER_MESSAGE,
        DashboardState,
        SecurityEvent,
        _ChatSlot,
        credential_records,
        crew_log_emit,
        effective_session_key,
        logger,
        redact_and_truncate,
        redact_credentials,
        redact_credentials_with_records,
        redact_exfiltration_urls,
        redact_exfiltration_urls_with_records,
        reflow_and_label_glued_option_marker,
        register_images_off_loop,
        register_widgets_off_loop,
        sel,
        sel_is_warm,
        telemetry_channel_of,
        with_bounded_redaction_records,
    )
    from kiro_crew.dashboard.chat_turn.decisions_hooks import _decisions_strip_meta
    from kiro_crew.dashboard.chat_turn.file_changes import _note_reply_row

#: Shapes that make glued footer text look like an instruction to a later
#: reader. Used only to tag the audit event; the label is applied regardless.
_DIRECTIVE_SHAPED_RE = re.compile(
    r"""(?ix)(?:^|[\s(\[{"'])(?:system|assistant|developer|instruction)\s*["']?\s*:"""
)


def _reflow_label_and_audit(slot: Any, text: str) -> str:
    """Repair text glued to an ``[OPTIONS:]`` footer and audit that it happened.

    The one call every persist seam makes: the reflow moves the glued text to
    its own line under :data:`kiro_crew.constants.GLUED_FOOTER_TEXT_LABEL`, and
    when anything was moved the SEL row is requested in the same step, so no
    seam labels without also asking for the audit. The audit itself is
    best-effort: :func:`_log_glued_footer_text` skips the row while SEL is cold
    (the label is the fix; the row only counts occurrences).
    """
    repaired, glued = reflow_and_label_glued_option_marker(text)
    if glued:
        _log_glued_footer_text(slot, glued)
    return repaired


def _log_glued_footer_text(slot: Any, glued: list[str]) -> None:
    """Audit text the model glued after its ``[OPTIONS:]`` footer.

    The footer is the message's last line by contract, so anything glued to it
    is output that ran past the contract -- most often harmless, sometimes a
    forged ``(system: ...)`` directive the model then treats as an attack on
    its next turn. The SEL row makes the occurrence visible to an operator
    without blocking anything; the label in the transcript does the rest.
    """
    if not sel_is_warm():
        logger.debug("glued footer text not logged: SEL not warm")
        return
    preview = redact_and_truncate(" | ".join(glued), 200)
    caller = getattr(slot, "_active_turn_session_key", "") or effective_session_key(slot)
    sel().log(
        SecurityEvent(
            event_id=uuid.uuid4().hex[:16],
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            event_type="output_anomaly",
            caller_identity=caller,
            agent=slot.agent or "kirocrew",
            source=telemetry_channel_of(caller),
            operation="options_footer_glued_text",
            outcome="labelled",
            metadata={
                "count": len(glued),
                "chars": sum(len(g) for g in glued),
                "preview": preview,
                "directive_shaped": bool(_DIRECTIVE_SHAPED_RE.search(" ".join(glued))),
            },
        )
    )


def _segment_row_meta(
    slot: _ChatSlot, blocked_links: list[dict], redactions: list[dict] | None = None
) -> dict | None:
    """The assistant row's ``meta``: the decision strip, redaction records and the
    segment's native message id, which this consumes.

    One function because every field has to reach ``slot.append`` in the same
    dict -- that call broadcasts the live frame from inside itself, so a field
    written onto the row afterwards would persist but be missing from the frame
    an open tab renders. Returns None when there is nothing to carry, which is
    what ``slot.append`` expects for a row with no meta.
    """
    meta = _decisions_strip_meta(slot)
    message_id = getattr(slot, "segment_message_id", "")
    if message_id:
        slot.segment_message_id = ""
    if not blocked_links and not redactions and not message_id:
        return meta
    out = dict(meta) if meta else {}
    if message_id:
        out["native_message_id"] = message_id
    if blocked_links:
        out["blocked_links"] = blocked_links
    if redactions:
        out["redactions"] = redactions
    return out


#: Past this the raw segment copy is abandoned and the flush describes only
#: what survived per-delta redaction.
_SEGMENT_RAW_MAX_CHARS = 2_000_000


def _accumulate_segment_raw(slot: _ChatSlot, text: str) -> None:
    """Append one streamed delta to the slot's raw segment copy."""
    raw = slot.segment_raw_text
    if raw is None:
        return
    raw += text
    slot.segment_raw_text = raw if len(raw) <= _SEGMENT_RAW_MAX_CHARS else None


def _cleared_segment_text(slot: _ChatSlot) -> str:
    """Clear the slot's raw segment copy and return the cleared streamed text.

    The one way a turn drops its streamed text mid-segment
    (``assistant_text = _cleared_segment_text(slot)``), so the raw copy that
    mirrors that text can never outlive it: records taken from a copy that
    outlived its text would describe addresses the reader never saw.
    """
    slot.segment_raw_text = ""
    return ""


def _redact_segment(slot: _ChatSlot, text: str) -> tuple[str, list[dict], list[dict]]:
    """Redact one assistant segment and describe every placeholder it wrote.

    The records come out of the redaction pass because that is the last moment
    the removed values exist: what ``slot.append`` stores is the placeholder,
    so a later scan of the row finds only that and would describe nothing.

    ``text`` was already redacted delta by delta as it streamed, so a value that
    arrived inside one delta is gone from it. The slot's raw copy of the same
    segment still holds it, so the raw copy's records are used wherever they
    pair with a placeholder ``text`` still carries: a link record by the domain
    its placeholder names, the credential records by position when both texts
    hold the same number of credential tags. A transform on ``text`` (label
    reflow, plan-marker stripping) or a delta boundary that cut a value
    differently changes neither pairing. Where they do not pair, ``text``'s own
    records stand, which describe fewer placeholders but never a wrong one.
    Returns ``(text, blocked_links, redactions)``.
    """
    raw = slot.segment_raw_text
    slot.segment_raw_text = ""
    redacted, blocked_links, redactions = _redact_segment_text(slot, text)
    if not raw or raw == text:
        return redacted, blocked_links, redactions
    raw_redacted, raw_links, raw_redactions = _redact_segment_text(slot, raw)
    if raw_redacted == redacted:
        return redacted, raw_links, raw_redactions
    seen = {r.get("url") or (r.get("domain"), r.get("path")) for r in blocked_links}
    for record in raw_links:
        key = record.get("url") or (record.get("domain"), record.get("path"))
        placeholder = f"[REDACTED: suspicious URL to {record.get('domain')}]"
        if key not in seen and placeholder in redacted:
            if len(blocked_links) >= MAX_BLOCKED_LINKS_PER_MESSAGE:
                break
            blocked_links.append(record)
            seen.add(key)
    tags = len(_CREDENTIAL_TAG_RE.findall(redacted))
    if tags and tags == len(_CREDENTIAL_TAG_RE.findall(raw_redacted)) == len(raw_redactions):
        redactions = raw_redactions
    return redacted, blocked_links, redactions


#: Every credential tag the redactor writes (the frontend pairs the same form).
_CREDENTIAL_TAG_RE = re.compile(r"\[REDACTED: (?:encoded )?credential\]")


def _redact_segment_text(slot: _ChatSlot, text: str) -> tuple[str, list[dict], list[dict]]:
    """Both removers over ``text``, with the records each placeholder needs."""
    # Never relaxed by the reader's allowed hosts: what is saved must not depend
    # on the allow-list, so an allowed link is kept as a placeholder with its
    # record and shown again at display time (chat_utils._prepare_messages).
    redacted, exfil_warnings, blocked_links = redact_exfiltration_urls_with_records(text)
    for w in exfil_warnings:
        logger.warning("Exfiltration URL redacted in chat segment: %s", w)
    redacted, cred_warnings, matches = redact_credentials_with_records(redacted)
    for w in cred_warnings:
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- the redactor's warning names the pattern class, never the matched value; moved verbatim
        logger.warning("Credential redacted in chat segment: %s", w)
    return redacted, blocked_links, credential_records(matches, slot.credential_evidence)


def _append_command_reply(slot: _ChatSlot, text: str) -> None:
    """Persist a local slash command's reply the way a model segment is persisted.

    A command answered here (``/workflow``, ``/goal``, ``/agent``, ``/prompts``,
    a harness command the backend cannot run) writes the same assistant row a
    model reply does, and its text can carry a credential or a suspicious link:
    an error string, a prompt description, or the user's own argument echoed
    back. Redacting that text without the records leaves each placeholder
    unexplained -- no lock tag card, no blocked-link chip, no one-time coach --
    which is the silent rewrite every model persist seam already avoids. So the
    text goes through the segment's own redaction pass and the records ride in
    the row's ``meta`` on the same ``slot.append`` call, so the live frame
    carries them too (see :func:`_segment_row_meta`). The segment's raw copy,
    message id and decision strip are left alone: they belong to the model's
    turn, not to a command answered without one.
    """
    redacted, blocked_links, redactions = _redact_segment_text(slot, text)
    meta: dict[str, Any] = {}
    if blocked_links:
        meta["blocked_links"] = blocked_links
    if redactions:
        meta["redactions"] = redactions
    slot.append("assistant", redacted, "msg msg-a", meta=meta or None)


#: Window rows a tool group puts between a segment's chunk rows: the call row,
#: the approval card a gated call appends, and a change card or guide offer.
_TOOL_GROUP_ROLES = frozenset({"tool", "permission", "card"})


def _flush_segment(
    state: DashboardState,
    slot: _ChatSlot,
    assistant_text: str,
    *,
    broadcast: bool = True,
    quiet_persist: bool = False,
    interrupted: bool = False,
) -> None:
    """Finalize current text block as a segment and persist it.

    ``quiet_persist`` additionally suppresses the per-message ``chat_message``
    broadcast that ``slot.append`` emits for the finalized assistant message.
    Used ONLY by the mid-turn steer cut: at that boundary every client has
    already finalized its streaming message (optimistic freeze on the
    initiating tab, steer_push freeze on the others), so a broadcast here
    would render a DUPLICATE copy of the pre-steer text below the steer
    bubble. Normal end-of-segment flushes keep the broadcast — there the
    clients still hold a live streaming message for it to reconcile into.

    The append-only log's identity for this segment is DERIVED here, not passed
    in: the ACP session id comes from the slot's own client, the turn from the
    emitter's live-turn record, and the model call from that turn's step counter.
    Three keyword arguments at six call sites was the alternative, and each one
    was a place to pass a stale ordinal -- which is the defect this shape removes
    rather than documents. Outside a turn the derivation yields no session and no
    turn, and the emitter records nothing, which is correct: a segment flushed
    outside a turn has no turn or model call to name.
    """

    # Remove trailing chunk messages (they belong to this segment).
    # Also pull aside any stop_event interleaved with this segment's chunks
    # so it lands AFTER the finalized assistant message. Historical
    # stop_events from prior turns stay in place.
    def _is_stop_event(m: dict) -> bool:
        cls_val = m.get("cls", "")
        if not cls_val or not isinstance(cls_val, str):
            return False
        try:
            parsed = json.loads(cls_val)
            return isinstance(parsed, dict) and parsed.get("kind") == "stop_event"
        except ValueError:
            return False

    # Walk backwards over THIS segment's rows and split them into: chunk rows
    # to drop, stop_events to re-land after the finalized message, and
    # tool-group rows (tool, permission, card) to keep in place. The walk stops
    # at the first row that is none of those — the previous persisted boundary —
    # which marks the segment start. The tool-group set is the one the
    # post-tool text fallback reads past: a permission row is the approval card
    # a gated call appends, a card row is a change card or guide offer the
    # group's own call puts in, and either can land between deferred chunks.
    #
    # A deferred cut (open_construct_at_end holds the boundary while a construct
    # is open) lets an interleaved tool call land BETWEEN this segment's chunk
    # rows, so some of the segment's chunks sit before a tool row rather than in
    # one trailing run. Removing every chunk row from the segment start onward
    # (not just a trailing run) clears them all; a chunk row left before a tool
    # row would strand in the window, where a slot-detail reload renders it as a
    # duplicate ``streaming`` bubble beside the finalized reply. The tool rows
    # that interleave the chunks are kept in order. The rows are wire-only and
    # never persisted, so this repairs the in-memory window only; nothing on
    # disk changes.
    segment_start = len(slot.messages)
    for i in range(len(slot.messages) - 1, -1, -1):
        role = slot.messages[i].get("role", "")
        if role == "chunk" or role in _TOOL_GROUP_ROLES or _is_stop_event(slot.messages[i]):
            segment_start = i
        else:
            break
    head = slot.messages[:segment_start]
    tail = slot.messages[segment_start:]
    trailing_stop_events = [m for m in tail if _is_stop_event(m)]
    # Keep the tool-group rows of this segment in place; drop its chunk rows
    # and pull its stop_events aside (re-landed after the finalized message).
    kept_tool_rows = [
        m for m in tail if m.get("role", "") in _TOOL_GROUP_ROLES and not _is_stop_event(m)
    ]
    slot.messages = head + kept_tool_rows
    # The window rewrite above is only half the release: append put each chunk
    # row in `_pending` as well, as the SAME dict, so the queue still owns every
    # token of this segment. This is the SUCCESS path — the one a long streamed
    # turn normally takes — so skipping it leaks the whole stream on any slot
    # that is not asked for another turn.
    slot.release_pending_chunks()
    # Repair a glued option marker before persisting. A mid-turn steer reply (or
    # any concatenation seam upstream) can append prose directly after an
    # ``[OPTIONS: ...]`` line with no separator, producing a single line the render
    # grammar cannot match (its closer is anchored to end-of-line), so the marker
    # leaks as literal text and loses its pills. Every finished segment passes
    # through here (an abnormally ended turn goes through _persist_partial_reply,
    # which applies the same repair), so the additive newline insert fixes the
    # stored transcript once, without touching the parse grammar. The moved text
    # is labelled as the assistant's own output and the event is audited: a
    # model that later re-reads the line must not take it for an instruction.
    assistant_text = _reflow_label_and_audit(slot, assistant_text)
    redacted, blocked_links, redactions = _redact_segment(slot, assistant_text)
    # Persist as assistant message. Broadcast is kept enabled so that
    # other tabs viewing the same slot receive the finalized text.
    # The active tab already has this content from streaming chunks;
    # the chat_segment event tells it to finalize streaming → assistant.
    _note_reply_row(
        slot,
        slot.append(
            "assistant",
            redacted,
            "msg msg-a",
            broadcast=not quiet_persist,
            # The decision strip, when this turn made one, and the blocked-link records
            # from the redaction above. Passed here rather than written onto the row
            # afterwards so the frame this call broadcasts carries them too -- see
            # _decisions_strip_meta for why that matters.
            meta=_segment_row_meta(slot, blocked_links, redactions),
        ),
    )
    # The append-only log's copy of the same body. Written here rather than at the
    # turn's terminal event because a turn produces SEVERAL assistant messages --
    # one per model call -- and the terminal event sees only the last. The identity
    # is read from the slot's client and the emitter's live record, so there is one
    # source for it rather than a copy threaded through every caller; the step is
    # derived inside the emitter from the same record.
    _crew_log_sid_here = crew_log_emit.session_id_of(getattr(slot, "_acp_client", None))
    crew_log_emit.on_message_sent(
        _crew_log_sid_here,
        crew_log_emit.live_turn(_crew_log_sid_here),
        text=redacted,
        interrupted=interrupted,
    )
    last_msg: dict = slot.messages[-1]
    # If a regenerate is pending, attach the stashed variants to this fresh assistant message.
    if slot._pending_variants:
        pending_list = []
        for v in slot._pending_variants:
            if not isinstance(v, dict):
                continue
            # A stashed variant's text is ALREADY redacted, so its records are
            # carried rather than derived: the URL they describe is gone from that
            # text and a rescan of it would return nothing, which would destroy
            # the only explanation the reader can switch back to. Records are
            # derived exactly once, at the redaction that writes the placeholder.
            # The removers still run -- they are idempotent, and a variant that
            # reached here unredacted must not be stored that way.
            v_text, _ = redact_exfiltration_urls(v.get("content", ""))
            entry = with_bounded_redaction_records({**v, "content": redact_credentials(v_text)[0]})
            pending_list.append(entry)
        newest: dict = {"content": redacted, "ts": last_msg.get("ts", "")}
        if blocked_links:
            newest["blocked_links"] = blocked_links
        if redactions:
            newest["redactions"] = redactions
        pending_list.append(newest)
        last_msg["variants"] = pending_list
        last_msg["variant_idx"] = len(pending_list) - 1
        slot._pending_variants = []
    # Re-append any stop_event that belongs to this segment's trailing run,
    # placed AFTER the finalized assistant message so the UI shows
    # prose → stop card.
    for ev in trailing_stop_events:
        slot.messages.append(ev)
    # Tell the frontend to finalize streaming → assistant.
    if broadcast:
        state.broadcast_ws("chat_segment", {"slot": slot.key})
        # Notify other tabs about variant metadata so they don't need a full refresh.
        # Use last_msg (the assistant message) not slot.messages[-1] which may be a
        # trailing stop_event appended after the assistant message.
        if last_msg.get("variants"):
            state.broadcast_ws(
                "chat_variant_switch",
                {"slot": slot.key, "index": last_msg.get("variant_idx", 0), "content": redacted},
            )
    # Auto-register any <mcwidget> in this segment as an (unpinned) artifact so
    # it appears in the session's Artifacts tab and the star becomes a pure
    # metadata flip. Registered from the REDACTED text — the artifact is a
    # dashboard-surfaced copy of the widget, so it must not persist a credential
    # the segment redaction just stripped out of chat.
    _schedule_widget_registration(state, slot, redacted, str(last_msg.get("ts", "")))


def _schedule_widget_registration(
    state: DashboardState,
    slot: _ChatSlot,
    text: str,
    message_ts: str,
) -> None:
    """Fire-and-forget widget auto-registration for a finalized segment.

    Detached deliberately: registration touches the artifact store (blocking
    filesystem work, offloaded to an executor inside
    ``register_widgets_off_loop``), and a widget artifact appearing a beat after
    the message renders is invisible to the user, whereas awaiting it would add
    store latency to every segment flush of every turn. Failures are logged by
    the callee and never surface into the turn.

    Spawned via ``asyncio.create_task`` (not ``loop.create_task``) to match every
    other detached task in this module — tests that neutralize background work
    patch ``chat_runner.asyncio.create_task``, and a task spawned off the loop
    handle directly would slip past that and run real store I/O mid-test.

    The no-running-loop case is guarded: with no loop, registration is skipped
    rather than raising into a segment flush (some callers in this module's
    history are sync, and a CLI/test path has no artifact-store expectations).

    **Restricted sessions never register.** Incognito / temporary slots
    (``slot.is_restricted``) are denied every artifact write at the HTTP gate
    (``_is_restricted_session``), so registering here would be a back door around
    that ceiling: widget HTML from a session the user expected to leave no trace
    would persist to ``artifacts/<slug>/`` and show up in the library. The gate
    keys off the SAME ``slot.is_restricted`` signal, so the two agree by
    construction.
    """
    if not text:
        return
    if getattr(slot, "is_restricted", False):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    # Store the BARE slot key, not "dashboard:<key>". The in-session Artifacts
    # tab queries ?session=<activeSlot>, which is the bare key, and
    # ``ArtifactStore.list`` compares ``session_key`` exactly (no prefix folding,
    # unlike ``_collect_session_docs``). WidgetFrame's fallback create also sends
    # the bare key, so this keeps auto-registered and star-created artifacts in
    # the same bucket — the one the tab can actually see.
    #
    # Two independent registration passes ride the same restricted-session gate
    # and off-loop dispatch: <mcwidget> bodies (inline HTML) and local markdown
    # images (bytes copied off disk). The cheap substring pre-checks keep a
    # plain prose segment from scheduling either task.
    if "<mcwidget" in text:
        task = asyncio.create_task(register_widgets_off_loop(text, message_ts, slot.key))
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
    if "![" in text:
        image_task = asyncio.create_task(register_images_off_loop(text, message_ts, slot.key))
        state._background_tasks.add(image_task)
        image_task.add_done_callback(state._background_tasks.discard)
