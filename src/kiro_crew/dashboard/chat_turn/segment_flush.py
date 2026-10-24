"""Finalizing a dashboard turn's assistant text segment: redaction with its records, row meta,
the variant stash, the [OPTIONS:] footer audit and the widget registration; a local command
reply is persisted through the same redaction; and the settle of the turn's answer once the
stream ends (``_turn_settle_answer``): the terminal directive, the final segment, the
refusal-fallback hop and refusal card, the post-compaction continuation and the empty-response
ladder."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import reflow_and_label_glued_option_marker
from kiro_crew.dashboard.chat_utils import (
    _ACTIVITY_NO_REPLY_CONTINUE_MSG,
    _COMPACTION_CONTINUE_MSG,
    _EMPTY_AUTO_CONTINUE_MSG,
    EMPTY_RUNG_CONTINUE,
    EMPTY_RUNG_GIVE_UP,
    EMPTY_RUNG_REPLAY,
    EMPTY_TURN_NOTICE_KIND,
    EmptyTurnActivity,
    RecoveryPayload,
    classify_empty_turn,
    is_false_current_tool_blocker_near_miss,
    normalize_stop_reason,
    payload_for_replay,
    should_continue_after_compaction,
    tool_calls_are_read_only_preparation,
    with_bounded_redaction_records,
)
from kiro_crew.dashboard.recovery_replays import replays_of
from kiro_crew.dashboard.state import chat_message_note, row_mid
from kiro_crew.messaging.empty_turn_copy import EMPTY_TURN_NOTICE, EMPTY_TURN_NOTICE_AFTER_WORK

# Read when the module loads (the placeholder pattern), so imported plainly.
from kiro_crew.security import EXFILTRATION_REDACTION_TAG_PREFIX, redact, redact_and_truncate
from kiro_crew.security.credential_sources import credential_records
from kiro_crew.security.redaction import redact_credentials_with_records
from kiro_crew.sel import SecurityEvent

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        EMPTY_TURN_NOTICE_AFTER_RECOVERY,
        REDACTED_CREDENTIAL_TAG,
        STOP_REASON_CANCELLED,
        STOP_REASON_END_TURN,
        STOP_REASON_REFUSAL,
        SYNTHETIC_RECOVERY_KIND,
        DashboardState,
        ReplayFamily,
        _ChatSlot,
        crew_log_emit,
        effective_session_key,
        logger,
        redact_credentials,
        redact_exfiltration_urls,
        redact_exfiltration_urls_with_records,
        register_images_off_loop,
        register_widgets_off_loop,
        sel,
        sel_is_warm,
        telemetry_channel_of,
    )
    from kiro_crew.dashboard.chat_turn.decisions_hooks import _decisions_strip_meta
    from kiro_crew.dashboard.chat_turn.file_changes import _note_reply_row
    from kiro_crew.dashboard.chat_turn.model_fallback import (
        _configured_refusal_fallback,
        _record_refused_cycle,
        _refusal_fallback_retry,
    )
    from kiro_crew.dashboard.chat_turn.recovery import (
        _answer_text_only,
        _empty_auto_continue_enabled,
        _empty_max_auto_continues,
        _should_suppress_requeue,
        refusal_card_text,
    )
    from kiro_crew.dashboard.chat_turn.steer_queue import _has_user_queued_followup
    from kiro_crew.dashboard.chat_turn.turn_entry import _session_stop_generation, _stop_pressed
    from kiro_crew.dashboard.chat_turn.turn_ledgers import _queue_recovery, _report_consumed
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn
    from kiro_crew.dashboard.chat_turn.turn_reply_seams import _flush_text_stream

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


#: Past this the raw segment copy is abandoned, and the flush saves
#: :data:`_SEGMENT_UNCHECKED_TEXT` instead of the segment (fail closed).
_SEGMENT_RAW_MAX_CHARS = 2_000_000

#: What a segment flush saves when it cannot redact the segment's raw copy as
#: one text: no raw copy (past the cap), a raw copy that does not match the
#: streamed text, a redaction pass that raised, or a value a per-delta pass
#: removed that the whole-text pass would keep, in the text or in a record.
_SEGMENT_UNCHECKED_TEXT = (
    "[REDACTED: reply withheld, it could not be checked for secrets."
    " Ask the agent to send it again.]"
)

#: Ceiling on the substring work the flush spends checking removed values
#: against the saved row (values times characters scanned). The flush runs on
#: the event loop, so a reply with tens of thousands of removed values must
#: not stall it; past this the segment fails closed instead.
_SEGMENT_CHECK_MAX_WORK = 400_000_000


def _reset_segment_copy(slot: _ChatSlot) -> None:
    """Drop the slot's raw segment copy and the checks that ride with it."""
    slot.segment_raw_text = ""
    slot.segment_shown_sha = hashlib.sha256()
    slot.segment_removed = {}


def _redact_delta(text: str) -> tuple[str, list[tuple[str, str]]]:
    """One streamed delta through both removers, as the run loop streams it.

    Returns ``(shown, removed)``: the redacted delta, and ``(value, tag)`` for
    every value the two passes replaced (each blocked address with its
    placeholder, each credential value with the credential tag). ``removed``
    is memory only and must never be stored, logged or sent.
    """
    removed: list[tuple[str, str]] = []
    shown, _, _ = redact_exfiltration_urls_with_records(text, removed_out=removed)
    shown, _, matches = redact_credentials_with_records(shown, defer_token_param=True)
    removed += [(m.value, REDACTED_CREDENTIAL_TAG) for m in matches if m.value]
    return shown, removed


def _accumulate_segment_raw(
    slot: _ChatSlot, text: str, shown: str, removed: Iterable[tuple[str, str]]
) -> None:
    """Append one streamed delta to the slot's raw segment copy.

    ``shown`` and ``removed`` are what :func:`_redact_delta` returned for it:
    the piece the run loop appends to the streamed text, and each value it
    replaced. The hash of ``shown`` lets the flush prove the raw copy is the
    copy of that text, and each removed value lets it keep that value out of
    the saved text too (see :func:`_redact_segment`).
    """
    raw = slot.segment_raw_text
    if raw is None:
        return
    raw += text
    if len(raw) > _SEGMENT_RAW_MAX_CHARS:
        slot.segment_raw_text = None
        slot.segment_removed = {}
        return
    slot.segment_raw_text = raw
    slot.segment_shown_sha.update(shown.encode("utf-8", "surrogatepass"))
    for value, tag in removed:
        slot.segment_removed.setdefault(value, tag)


def _cleared_segment_text(slot: _ChatSlot) -> str:
    """Clear the slot's raw segment copy and return the cleared streamed text.

    The one way a turn drops its streamed text mid-segment
    (``assistant_text = _cleared_segment_text(slot)``), so the raw copy that
    mirrors that text can never outlive it: a copy that outlived its text would
    be saved in place of text the reader never saw.
    """
    _reset_segment_copy(slot)
    return ""


def _hold_segment_tail(slot: _ChatSlot, text: str, tail: str) -> str | None:
    """Leave *tail* off the slot's raw segment copy and return *text* without it.

    *tail* is raw text the live stream still withholds at a display boundary
    inside the reply (a tool call, an approval prompt), and *text* is the
    segment as it streamed. The cut keeps the copy the copy of the text it
    returns, so the flush's checks hold for the shorter segment. None when the
    copy and *text* do not both end with *tail* as it was written: the two
    cannot then be cut at the same place, and the caller saves the segment
    whole.
    """
    raw = slot.segment_raw_text
    if not tail or raw is None or not raw.endswith(tail) or not text.endswith(tail):
        return None
    kept = text[: len(text) - len(tail)]
    slot.segment_raw_text = raw[: len(raw) - len(tail)]
    slot.segment_shown_sha = hashlib.sha256(kept.encode("utf-8", "surrogatepass"))
    return kept


def _open_segment_with(slot: _ChatSlot, text: str, head: str) -> str:
    """Put *head* at the start of the slot's raw segment copy; return *text* with it in front.

    *head* is raw text the live stream held at a display boundary that turned
    out to be part of what followed it, and *text* the segment streamed since.
    It goes through the per-delta removers like any streamed delta, so the
    flush redacts the two as one text, as the live stream did.
    """
    shown, removed = _redact_delta(head)
    text = shown + text
    raw = slot.segment_raw_text
    if raw is not None:
        raw = head + raw
        if len(raw) > _SEGMENT_RAW_MAX_CHARS:
            slot.segment_raw_text = None
            slot.segment_removed = {}
        else:
            slot.segment_raw_text = raw
            for value, tag in removed:
                slot.segment_removed.setdefault(value, tag)
    slot.segment_shown_sha = hashlib.sha256(text.encode("utf-8", "surrogatepass"))
    return text


def _give_back_run(
    state: DashboardState,
    slot: _ChatSlot,
    row: dict[str, Any],
    text: str,
    variant_idx: int | None,
) -> None:
    """Put *text* back at the end of *row*, the segment row it was held off, and tell open tabs.

    *text* is what the live stream released for a run it held at a display
    boundary, once the run proved to be a word of its own. *variant_idx* is the
    row's variant when the run was cut off it: a row showing another variant
    since is left as it is. The text is appended to the content the row holds
    now, which a save may have rewritten in place (an inline image's path).

    *text* is also queued on the slot's delivery queue as the ``chunk`` it was
    held back from: the /api/chat SSE stream and the OpenAI-compatible replies
    build the reply from the chunks they drain and never see a row patch. It
    goes on the queue only, not in the window (the row already holds it) and
    not on the WebSocket (open tabs take the patch below).

    The rebuilt content gets the glued-footer repair the row's save made: a run
    given back after an ``[OPTIONS:]`` footer is prose glued to it.
    """
    if not text:
        return
    slot.push_wire_frame("chunk", text)
    if row.get("variant_idx") != variant_idx:
        return
    before = row.get("content") or ""
    content = _reflow_label_and_audit(slot, before + text)
    row["content"] = content
    variants = row.get("variants")
    if (
        isinstance(variants, list)
        and isinstance(variant_idx, int)
        and 0 <= variant_idx < len(variants)
        and isinstance(variants[variant_idx], dict)
        and variants[variant_idx].get("content") == before
    ):
        variants[variant_idx]["content"] = content
    slot._dirty = True
    # In the form the row's own frame carried it (allowed links shown, display
    # pass run), so an open tab and a reload agree on the row.
    shown = chat_message_note(slot.key, row, workspace=getattr(slot, "workspace", None))
    state.broadcast_ws(
        "chat_message_update",
        {
            "slot": slot.key,
            "ts": row.get("ts", ""),
            "mid": row_mid(row),
            "content": shown["content"],
        },
    )


def _redact_segment(slot: _ChatSlot, text: str) -> tuple[str, list[dict], list[dict]]:
    """Redact one assistant segment as one text and describe every placeholder.

    ``text`` is the segment as it streamed: each delta redacted on its own.
    A delta boundary can cut a value, so per-delta redaction can leave pieces
    of it (a link's query after the placeholder its first piece became). So
    what is saved is the slot's raw copy of the same deltas, with the same
    glued-footer reflow, redacted once as a whole: every value is intact there,
    and the records come out of that one pass, so each describes a placeholder
    the saved text holds.

    Fail closed: when that cannot be done with certainty, the saved text is
    :data:`_SEGMENT_UNCHECKED_TEXT` with no records, never the raw text. That
    is when there is no raw copy (past :data:`_SEGMENT_RAW_MAX_CHARS`), when the
    raw copy is not the copy of ``text`` (its per-delta hash differs), or when
    a redaction pass raises.

    A pass is not monotone in its context: a value a per-delta pass removed
    (a credential at a delta start, a piece of base64) can look harmless in the
    whole text. When the whole-text result holds any such value (checked on
    its own) more often than ``text``'s own redaction does, or shows any run of
    text that redaction does not (:func:`_saved_text_shows_more`, default
    refuse: main's save is the only proof a run is safe), the flush fails
    closed. A blocked-link record is kept only for a placeholder the saved text
    holds and when it quotes nothing but main's text or an address a delta
    blocked whole (:func:`_links_main_shows`). The records get the same rule: a
    saved record that holds a value a per-delta pass removed as a credential (a
    blocked link's ``url``, a credential record's source or view command, say)
    fails the flush closed too (:func:`_records_hold_removed_credential`). The
    raw copy is never edited to drop a value first: a mask changes what the
    next pass matches (a tag's space ends a link before its query), so what is
    saved is always one pass over what the model wrote, or the notice. So no
    value a per-delta pass removed is saved where ``text``'s own redaction
    would not hold it.

    With no streamed deltas (an empty raw copy) there is nothing to cut, and
    ``text`` itself is redacted. ``text`` is reflowed and audited here
    (:func:`_reflow_label_and_audit`), so callers pass it as streamed.
    Returns ``(text, blocked_links, redactions)``.
    """
    raw = slot.segment_raw_text
    shown_sha = slot.segment_shown_sha
    removed = slot.segment_removed
    _reset_segment_copy(slot)
    shown = _reflow_label_and_audit(slot, text)
    if raw == "":
        return _redact_segment_text(slot, shown)
    if not text:
        # Streamed deltas with no streamed text left: nothing was shown.
        return "", [], []
    reason = ""
    if raw is None:
        reason = "no raw copy (segment past the size cap)"
    elif shown_sha.digest() != hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest():
        reason = "raw copy does not match the streamed text"
    else:
        try:
            whole = reflow_and_label_glued_option_marker(raw)[0]
            saved = _redact_segment_text(slot, whole)
            if removed:
                per_delta, _, _ = _redact_segment_text(slot, shown)
                # Never edit the raw copy to drop such a value: a mask changes
                # what the next pass sees (a tag's space ends a link early).
                # The saved text is one pass over what the model wrote, or the
                # segment is withheld.
                reason = _whole_text_refusal(saved, per_delta, removed)
                if reason and saved[0] != (strict := _redact_segment_text(slot, whole, False))[0]:
                    # A delta boundary split an SRI value, so its delta redacted
                    # the bare digest the whole text keeps. One pass with that
                    # exemption off is main's verdict on the digest; save it if
                    # it passes the same checks, rather than withhold the reply.
                    if not _whole_text_refusal(strict, per_delta, removed):
                        saved, reason = strict, ""
        except Exception as exc:  # noqa: BLE001 -- any failure must fail closed
            # The type only: a message or traceback could quote the text.
            reason = f"redaction raised {type(exc).__name__}"
        if not reason:
            if not removed:
                return saved
            return saved[0], _links_main_shows(saved[1], saved[0], per_delta, removed), saved[2]
    logger.warning("segment flush failed closed: %s", reason)
    return _SEGMENT_UNCHECKED_TEXT, [], []


def _whole_text_refusal(
    saved: tuple[str, list[dict], list[dict]], per_delta: str, removed: dict[str, str]
) -> str:
    """Why the whole-text ``saved`` may not be saved over ``per_delta``, or ``""``."""
    if len(removed) * (len(saved[0]) + len(per_delta)) > _SEGMENT_CHECK_MAX_WORK:
        return "too many removed values to check"
    if _kept_removed_values(saved[0], per_delta, removed):
        return "whole-text pass keeps a value a per-delta pass removed"
    if _saved_text_shows_more(saved[0], per_delta):
        return "whole-text pass keeps text the streamed redaction hid"
    if _records_hold_removed_credential(saved[1], removed, saved[2]):
        return "a saved record holds a value a per-delta pass removed"
    return ""


def _kept_removed_values(saved: str, per_delta: str, removed: Iterable[str]) -> list[str]:
    """The removed values ``saved`` holds more often than ``per_delta`` does."""
    return [v for v in removed if saved.count(v) > per_delta.count(v)]


#: A run of text between whitespace. Punctuation inside a credential (a
#: connection string's ``:p(ss)@``, a quoted or bracketed password) stays in
#: its run, so a hidden value cannot be rebuilt from pieces that other text in
#: the reply happens to contain. The saved text is compared with main's save
#: run by run.
_SEGMENT_RUN_RE = re.compile(r"\S+")

#: A blocked-link placeholder, its domain captured.
_LINK_PLACEHOLDER_RE = re.compile(
    re.escape(EXFILTRATION_REDACTION_TAG_PREFIX) + r"([^\]\s]{1,253})\]"
)


def _blank_tags(text: str) -> str:
    """``text`` with every placeholder set off as its own run: the credential tag
    blanked, a blocked-link placeholder replaced by its domain alone (the reader
    sees the domain). Text glued to a placeholder becomes a run of its own."""
    text = text.replace(REDACTED_CREDENTIAL_TAG, " ")
    return _LINK_PLACEHOLDER_RE.sub(lambda m: f" {m.group(1)} ", text)


def _shows_only(text: str, main: str) -> bool:
    """Whether every run of ``text`` appears in ``main`` (False past the ceiling)."""
    unseen = set(_SEGMENT_RUN_RE.findall(text)) - set(_SEGMENT_RUN_RE.findall(main))
    if len(unseen) * len(main) > _SEGMENT_CHECK_MAX_WORK:
        return False
    return all(run in main for run in unseen)


def _saved_text_shows_more(saved_text: str, main_text: str) -> bool:
    """Whether the whole-text save shows text main's per-delta save does not.

    Default refuse: main's save (the streamed text redacted again) is the only
    proof that a piece of text is safe to keep. Every run
    (:data:`_SEGMENT_RUN_RE`) of the saved text, placeholder domains included,
    must appear in main's text; one that does not is text a delta's redaction
    hid, and the segment fails closed. Nothing else vouches for a run (no
    blocked address, no placeholder, no record), so no shape of glued, quoted
    or forged text can launder a hidden value. A check past
    :data:`_SEGMENT_CHECK_MAX_WORK` answers True without scanning.
    """
    return not _shows_only(_blank_tags(saved_text), _blank_tags(main_text))


def _links_main_shows(
    blocked_links: list[dict], saved_text: str, main_text: str, removed: dict[str, str]
) -> list[dict]:
    """The saved blocked-link records whose quoted fields main's save shows.

    A field may be a piece of main's text or of an address a per-delta pass
    blocked whole (main's flush describes that address the same way). A record
    is the explanation for a placeholder, never the removal, so one quoting
    anything else is dropped and its placeholder stays.
    """
    main = _blank_tags(main_text)
    addresses = [v for v, tag in removed.items() if tag != REDACTED_CREDENTIAL_TAG]
    kept: list[dict] = []
    for record in blocked_links:
        # A record explains a placeholder the saved text holds; one whose
        # placeholder a later match swallowed explains nothing and is dropped.
        tag = f"{EXFILTRATION_REDACTION_TAG_PREFIX}{record.get('domain')}]"
        if saved_text.count(tag) <= sum(r.get("domain") == record.get("domain") for r in kept):
            continue
        if all(
            field in main or any(field in address for address in addresses)
            for field in _record_quoted_fields([record], ())
        ):
            kept.append(record)
    return kept


#: The blocked-link record fields that quote the reply itself. Every other
#: record field (``rule``, ``query_chars``, ``url_withheld``) is written by the
#: removers.
_BLOCKED_LINK_QUOTED_FIELDS = ("url", "path", "domain")

#: The credential record fields that quote a tool command or a file. Its
#: ``rule``, ``label`` and ``ordinal`` are written by the remover.
_CREDENTIAL_QUOTED_FIELDS = ("view_command", "profile_command")
_CREDENTIAL_SOURCE_QUOTED_FIELDS = ("command", "path", "section")


def _record_quoted_fields(
    blocked_links: Iterable[Any], credential_records: Iterable[Any]
) -> list[str]:
    """Every string field of the saved records that quotes the reply or a source."""
    fields = [
        value
        for record in blocked_links
        if isinstance(record, dict)
        for key in _BLOCKED_LINK_QUOTED_FIELDS
        if isinstance(value := record.get(key), str)
    ]
    for record in credential_records:
        if not isinstance(record, dict):
            continue
        source = record.get("source")
        fields += [v for k in _CREDENTIAL_QUOTED_FIELDS if isinstance(v := record.get(k), str)]
        if isinstance(source, dict):
            fields += [
                v for k in _CREDENTIAL_SOURCE_QUOTED_FIELDS if isinstance(v := source.get(k), str)
            ]
    return fields


def _records_hold_removed_credential(
    blocked_links: Iterable[Any],
    removed: dict[str, str],
    credential_records: Iterable[Any] = (),
) -> bool:
    """Whether a saved record quotes a credential a per-delta pass removed.

    The records ride in the row's ``meta`` and are shown and copied like the
    text, so they get the same rule: no value a per-delta pass removed as a
    credential may be saved in them. Only the fields that quote the reply or a
    credential's source (:func:`_record_quoted_fields`) are read, and a value
    counts only when it is a credential on its own (the credential remover
    changes it alone), so a short piece a delta cut off a ``token=`` value does
    not match a field by chance. A blocked address is expected in a record, so
    only values tagged :data:`REDACTED_CREDENTIAL_TAG` are checked. A check
    that would pass :data:`_SEGMENT_CHECK_MAX_WORK` answers True (fail closed)
    without scanning.
    """
    secrets = [v for v, tag in removed.items() if tag == REDACTED_CREDENTIAL_TAG]
    if not secrets:
        return False
    fields = _record_quoted_fields(blocked_links, credential_records)
    if len(secrets) * sum(len(f) for f in fields) > _SEGMENT_CHECK_MAX_WORK:
        return True
    return any(
        redact_credentials(v)[0] != v for v in secrets if any(v in field for field in fields)
    )


def _redact_segment_text(
    slot: _ChatSlot, text: str, keep_sri_digests: bool = True
) -> tuple[str, list[dict], list[dict]]:
    """Both removers over ``text``, with the records each placeholder needs."""
    # Never relaxed by the reader's allowed hosts: what is saved must not depend
    # on the allow-list, so an allowed link is kept as a placeholder with its
    # record and shown again at display time (chat_utils._prepare_messages).
    redacted, exfil_warnings, blocked_links = redact_exfiltration_urls_with_records(text)
    for w in exfil_warnings:
        logger.warning("Exfiltration URL redacted in chat segment: %s", w)
    redacted, cred_warnings, matches = redact_credentials_with_records(
        redacted, keep_sri_digests=keep_sri_digests
    )
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
) -> dict[str, Any]:
    """Finalize current text block as a segment, persist it, and return its row.

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
    # _redact_segment applies it (_reflow_label_and_audit) to both the streamed
    # text and the raw copy it saves.
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
    return last_msg


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


async def _turn_settle_answer(turn: _ChatTurn) -> None:
    """Settle the answer: terminal directive, final segment, refusal hop, continuation, empty ladder."""
    _activity_after_terminal = turn._activity_after_terminal
    _builtin_identity_trusted = turn._builtin_identity_trusted
    _compaction_completed = turn._compaction_completed
    _compaction_notice_chunks = turn._compaction_notice_chunks
    _compaction_started = turn._compaction_started
    _compaction_text_offset = turn._compaction_text_offset
    _is_synthetic = turn._is_synthetic
    _prompt_depth = turn._prompt_depth
    _recovering_compaction = turn._recovering_compaction
    _refusal_reasons = turn._refusal_reasons
    _retrying_empty = turn._retrying_empty
    _saw_terminal_event = turn._saw_terminal_event
    _saw_text_chunk = turn._saw_text_chunk
    _stop_reason = turn._stop_reason
    _terminal_synthetic = turn._terminal_synthetic
    _turn_billed = turn._turn_billed
    _turn_credits = turn._turn_credits
    _turn_flushed_visible_text = turn._turn_flushed_visible_text
    _turn_refusal = turn._turn_refusal
    _turn_successful_tool_call_ids = turn._turn_successful_tool_call_ids
    _turn_thought = turn._turn_thought
    _turn_tool_calls = turn._turn_tool_calls
    _turn_tool_identities = turn._turn_tool_identities
    first_word = turn.first_word
    full_message = turn.full_message
    is_new = turn.is_new
    message = turn.message
    resumed = turn.resumed
    slot = turn.slot
    state = turn.state
    # What the turn produced OF ITS OWN: `assistant_text` minus any backend
    # control notice that arrived as assistant text (the claude adapter's
    # "Compacting..."). The notice stays in `assistant_text` — it is real
    # output and must stream, flush and persist — but it is not an answer,
    # and the branch below is what decides whether one was given.
    #
    # A run the live stream held at the turn's last tool call or approval
    # prompt is settled first: nothing more will join it, and it either goes
    # back on the row it was cut from or opens this segment's text.
    if turn._held_runs:
        _flush_text_stream(turn)
    _answer_text = _answer_text_only(turn.assistant_text, _compaction_notice_chunks)
    turn._answer_text = _answer_text
    _continuation_answer_text = _answer_text_only(
        turn.assistant_text[_compaction_text_offset:], _compaction_notice_chunks
    )
    # A refusal's explanation answers the turn wherever it streamed: one that
    # arrived before a compaction still takes the streamed-explanation refusal
    # path below, instead of the no-text refusal branch, which would retry the
    # message on the fallback model and word its card as if nothing was said.
    _branch_answer_text = (
        _answer_text if _stop_reason == STOP_REASON_REFUSAL else _continuation_answer_text
    )

    # A turn with no answer after compaction still has to preserve every
    # pre-compaction fragment and control notice, but it must not take the
    # answer branch:
    # the post-compaction continuation is an `elif` UNDER that branch, so
    # entering it would shadow the continuation and leave the request
    # unanswered — the exact hang this PR exists to fix. Flush here and fall
    # through to the chain. (This is also why the notice cannot simply be
    # kept out of `assistant_text`: the answer branch owns the only terminal
    # `_flush_text_stream`, and the rolling redactor withholds the notice
    # until something flushes it, so a skipped notice was emitted nowhere.)
    if turn.assistant_text and not _branch_answer_text:
        _flush_text_stream(turn)
        _flush_segment(state, slot, turn.assistant_text, broadcast=False)
        turn._reply_flushed = True

    if turn._terminal_directive_applied and _activity_after_terminal:
        # Turn-end contract: a terminal directive is the LAST thing in its
        # turn. Text or a tool call after it is the model's violation, not
        # the runner's — the turn still ends cleanly and recovery stays
        # off (re-arming it would turn a quiet end into a notice card),
        # so this is reported once and nowhere else. Late text was dropped
        # at arrival (see _on_text_chunk in stream_chunks), so this count is the
        # only trace it leaves. Counts only, by contract: no text, no tool
        # names.
        logger.warning(
            "Turn-end contract violation for slot %s: %d model event(s) "
            "(text chunks or tool calls) after the terminal directive %r applied.",
            slot.key,
            _activity_after_terminal,
            turn._terminal_directive_kind,
        )

    if _branch_answer_text:
        _flush_text_stream(turn)
        _flush_segment(state, slot, turn.assistant_text, broadcast=False)
        turn._reply_flushed = True
        if _stop_reason == STOP_REASON_REFUSAL:
            # The Kiro service's content filter STREAMS its canned
            # explanation as assistant text and then ends the turn, so the
            # refusal reaches this (answered) branch rather than the
            # text-less one below. The text is kept -- it is what the
            # provider said. When agent.refusal_fallback_model names a
            # different model, this ONE message is retried on it (the
            # notice card replaces the terminal card); otherwise the
            # structured card follows the text so the user sees the
            # category and knows a same-model retry will not help.
            if await _refusal_fallback_retry(turn):
                pass
            else:
                _record_refused_cycle(turn)
                logger.warning(
                    "Model refusal for slot %s (category=%s) after "
                    "streamed explanation — not retrying",
                    slot.key,
                    (_turn_refusal.category or "-") if _turn_refusal else "-",
                )
                _refusal_card = refusal_card_text(
                    _turn_refusal,
                    streamed_text=turn.assistant_text,
                    fallback_off=not _configured_refusal_fallback(),
                )
                if slot._refusal_fallback_attempted:
                    # This refusal came from the retry turn itself: the
                    # configured fallback also declined. Say so — the card
                    # alone would read as though no retry ever ran — and
                    # name the model, so the user knows what to reconfigure.
                    # Same redaction + bound as the retry notice: the id
                    # comes from config or the provider envelope.
                    _fb = slot._refusal_fallback_candidate
                    if _fb:
                        _fb = redact(str(_fb))
                        _fb = _fb[:120]
                    _refusal_card += (
                        f" The configured fallback model ('{_fb}') also declined this request."
                        if _fb
                        else " The configured fallback model also declined this request."
                    )
                slot.append(
                    "error",
                    _refusal_card,
                    "msg msg-err",
                )
    elif _stop_reason == STOP_REASON_REFUSAL:
        # Model-side content refusal with no accompanying text: Anthropic's
        # bare `refusal` stop reason (passed through by every harness), or
        # the Kiro service's filter when kiro-cli surfaced only the
        # `_kiro.dev/metadata` envelope. The ACP layer folds both onto
        # STOP_REASON_REFUSAL + `AcpEvent.refusal`. This is
        # DETERMINISTIC FOR ONE MODEL — a blind same-model retry just
        # re-hits the same refusal and burns credits — so either retry the
        # message once on the configured refusal-fallback MODEL (a
        # different family routinely accepts what another's filter
        # declined), or surface a distinct, non-retried card. A refusal
        # on turn 1 with zero tool calls and no visible output usually points
        # at what we PREPENDED (persona / injected context / replay), not the
        # user's text; log the turn shape at WARNING. The prompt text
        # itself — even redacted and truncated — stays out of the service
        # log: prompts can carry secrets, and the shape fields answer the
        # prepended-vs-user-text question without the content.
        if await _refusal_fallback_retry(turn):
            pass
        else:
            _record_refused_cycle(turn)
            logger.warning(
                "Model refusal for slot %s (category=%s) — not retrying "
                "[is_new=%s resumed=%s tool_calls=%d visible_output=%s "
                "prompt_bytes=%d]",
                slot.key,
                (_turn_refusal.category or "-") if _turn_refusal else "-",
                is_new,
                resumed,
                _turn_tool_calls,
                turn._produced_visible_output,
                len(full_message),
            )
            _refusal_card = refusal_card_text(
                _turn_refusal, fallback_off=not _configured_refusal_fallback()
            )
            if slot._refusal_fallback_attempted:
                # This refusal came from the retry turn itself: the
                # configured fallback also declined. Say so — the card
                # alone would read as though no retry ever ran — and
                # name the model, so the user knows what to reconfigure.
                # Same redaction + bound as the streamed-refusal branch:
                # the id comes from config or the provider envelope.
                _fb = slot._refusal_fallback_candidate
                if _fb:
                    _fb = redact(str(_fb))
                    _fb = _fb[:120]
                _refusal_card += (
                    f" The configured fallback model ('{_fb}') also declined this request."
                    if _fb
                    else " The configured fallback model also declined this request."
                )
            slot.append(
                "error",
                _refusal_card,
                "msg msg-err",
            )
    elif not turn._terminal_directive_applied and should_continue_after_compaction(
        # A turn that ended on an applied terminal directive is answered by
        # that directive, whatever compaction happened before it: the empty
        # segment that the late-text drop in _on_text_chunk (stream_chunks) leaves is
        # the quiet end, not an abandoned request, so no continuation — same guard as the
        # empty-response arm below.
        # The context window filled mid-turn, the backend summarized, and the
        # turn then ended without finishing the request — the "hangs after
        # Compacting..." symptom. kiro-cli self-heals (it re-sends the pending
        # request once compaction settles, see `handle_compaction_loop_event`);
        # the Claude backend does NOT, so the resume has to be injected here.
        #
        # Placed BEFORE the empty-response ladder below deliberately: that
        # ladder's first rung silently replays the ORIGINAL prompt, which on a
        # compaction-interrupted turn restarts work that already completed and
        # is out of context now. This arm goes straight to a CONTINUATION
        # payload instead, telling the model the summary above is authoritative.
        compaction_started=_compaction_started,
        # COMPLETED only, never merely "settled": a failed compaction also
        # reaches a terminal, and continuing after one would tell the model a
        # summary it can rely on exists when the context was never summarized.
        compaction_settled=_compaction_completed,
        # An explicit `/compact` IS the user's whole request; it ended exactly
        # as asked, so there is nothing pending to continue.
        user_requested_compaction=(first_word == "/compact"),
        # The turn's own answer (see `_answer_text` above), not the raw
        # segment: this gate must not mistake "the backend said it was
        # compacting" for "the request was answered".
        final_segment_text=_continuation_answer_text,
        stop_reason=_stop_reason,
        end_turn_reason=STOP_REASON_END_TURN,
        prompt_depth=_prompt_depth,
        compaction_continue_retries=slot._compaction_continue_retries,
        is_cancelled=(_stop_reason == STOP_REASON_CANCELLED),
        refusal_reasons=_refusal_reasons,
        # Same user-intent gates as the promise-only arm in _turn_ending_chain,
        # for the same reasons: a Stop pressed during compaction can surface here
        # as a plain end_turn, and a user follow-up already queued must win over
        # a synthetic continuation rather than be jumped ahead of.
        stop_in_progress=_should_suppress_requeue(slot),
        # The live Stop signal every end-of-turn gate shares: the slot's own
        # counter AND the session-scoped one, so a stop issued on a linked
        # channel surface is seen here too.
        stop_generation_unchanged=not _stop_pressed(turn),
        queue_empty=not _has_user_queued_followup(slot),
        no_pending_steers=(not getattr(slot, "_pending_steers", None)),
    ):
        slot._compaction_continue_retries += 1
        logger.info(
            "Post-compaction stall for slot %s — context was summarized "
            "mid-turn and the turn ended without finishing; injecting one "
            "continuation (tool_calls=%d credits=%.4f)",
            slot.key,
            _turn_tool_calls,
            _turn_credits,
        )
        slot.append(
            "notice",
            "ℹ️ The context was compacted mid-turn and the response stopped "
            "there — continuing automatically.",
            "msg msg-info",
            meta={"kind": EMPTY_TURN_NOTICE_KIND},
        )
        _queue_recovery(
            turn,
            0,
            _COMPACTION_CONTINUE_MSG,
            kind=SYNTHETIC_RECOVERY_KIND,
            payload=RecoveryPayload.CONTINUATION,
        )
        # Snapshot for the dispatch-point purge, same as the promise-only arm
        # in _turn_ending_chain: catches a Stop that pressed AND resolved back
        # to idle while the continuation sat in the queue.
        replays_of(slot).arm(
            ReplayFamily.CONTINUATION,
            entry_id="",
            session_key=effective_session_key(slot),
            stop_gen=getattr(slot, "_stop_generation", 0),
            session_stop_gen=_session_stop_generation(turn),
        )
        _recovering_compaction = True
        turn._recovering_compaction = _recovering_compaction
    elif (
        _stop_reason != STOP_REASON_CANCELLED
        and not turn._produced_visible_output
        and not turn._terminal_directive_applied
        and not _refusal_reasons
    ):
        _had_empty_response_verdict = True
        turn._had_empty_response_verdict = _had_empty_response_verdict
        # Model returned an empty response — retry once, then notify user.
        # Precedence: a turn that ended on a recoverable tool refusal also has
        # empty assistant_text when the model went straight to the blocked
        # tool with no preamble. That is NOT a blind-retry case — re-running
        # the same message just re-hits the same gate. The `not _refusal_reasons`
        # guard lets it fall through to _turn_stop_hook's refusal-recovery path,
        # which hands the model the reason so it can adapt instead of looping.
        #
        # "Empty" here means only "the final assistant segment is empty", and
        # that is NOT the same as "the turn did nothing". `assistant_text` is
        # reset at every tool boundary, so a turn that answered and then called
        # a tool arrives here with its answer already flushed, persisted and
        # read — and `_produced_visible_output` does not cover that, by design
        # (its narrow meaning is load-bearing for the promise-only guard).
        # A tool-only turn arrives here too. The activity snapshot below is what
        # separates those from a provider that genuinely returned nothing.
        _empty_activity = EmptyTurnActivity(
            saw_terminal=_saw_terminal_event,
            terminal_synthetic=_terminal_synthetic,
            stop_reason=normalize_stop_reason(_stop_reason),
            saw_text=_saw_text_chunk,
            flushed_visible=_turn_flushed_visible_text,
            had_tools=_turn_tool_calls > 0,
            had_thinking=_turn_thought,
            billed=_turn_billed,
        )
        _empty_cause = classify_empty_turn(_empty_activity)
        # Snapshot BEFORE the rungs, each of which may increment the counter.
        # This is the ordinal of the turn just observed, which is what the
        # predecessor warning reported.
        _empty_attempt = slot._empty_response_retries + 1
        # THE load-bearing guard. A productive turn must never have its
        # originating message replayed verbatim: rung 1 below re-queues
        # `message` itself, which on such a turn re-runs tool calls that
        # already completed (a second `send_message`, a second write, a second
        # PR) and re-derives an answer the user has already read. A productive
        # turn skips to the continuation rung, which tells the model the work
        # above already happened.
        _may_replay_verbatim = not _empty_activity.productive
        if _prompt_depth == 0 and slot._empty_response_retries < 1 and _may_replay_verbatim:
            _empty_rung = EMPTY_RUNG_REPLAY
            # Seamless self-heal: silently re-queue on the first empty
            # response. An ephemeral status indicator is not used here — it
            # is emitted at turn-teardown and the frontend drops it once the
            # streaming turn ends (so it never surfaces). Only the second
            # consecutive empty surfaces a persisted notice card below.
            slot._empty_response_retries += 1
            # Retract BEFORE building the retry entry. The entry copies an
            # unsettled consumption callback; copying while the preceding
            # turn-complete report is still True would drop that callback
            # and strand a durable producer after the replay succeeds.
            await _report_consumed(turn, False)
            _queue_recovery(
                turn,
                0,
                message,
                kind=SYNTHETIC_RECOVERY_KIND,
                # Verbatim replay: ORIGINAL only if the incoming text was the
                # user's. On a recovery turn it is the runner's continuation.
                payload=payload_for_replay(_is_synthetic),
            )
            _retrying_empty = True
            turn._retrying_empty = _retrying_empty
        elif (
            _prompt_depth == 0
            and slot._empty_response_retries < 1 + _empty_max_auto_continues()
            and not _should_suppress_requeue(slot)
            and _empty_auto_continue_enabled()
        ):
            # Second consecutive empty: the silent SAME-message re-queue
            # also produced nothing. Re-sending the identical prompt tends
            # to reproduce the identical empty generation, but a DIFFERENT
            # message reliably recovers (observed repeatedly in the field —
            # the user typing "continue" broke the pattern every time). So
            # auto-send a synthetic continue nudge on the same live
            # session, with a transcript-visible notice so the recovery is
            # never invisible. The budget is
            # session.empty_response_max_continues (default 1 — one nudge,
            # the original behavior); when it is spent, the give-up notice
            # below fires — bounded, no loop.
            # A productive turn skipped the verbatim-replay rung entirely,
            # so its counter jumps past the replay slot (0 → 2) on its
            # first continuation and counts normally from there. That
            # keeps the give-up arithmetic uniform across both paths —
            # replay-or-jump plus the continue budget — and at the default
            # budget it is exactly the old jump-to-2: the single
            # continuation consumes the remaining budget, so a counter
            # left at 1 would run `_EMPTY_AUTO_CONTINUE_MSG` next,
            # contradicting both the notice ("continuing once") and the
            # side-effect boundary this branch protects.
            _max_continues = _empty_max_auto_continues()
            if _empty_activity.productive:
                slot._empty_response_retries = max(slot._empty_response_retries + 1, 2)
                # This turn's tools landed; later turns of the episode must know.
                slot._empty_episode_productive = True
            else:
                slot._empty_response_retries += 1
            # Ordinal of THIS continuation (1-based): the counter minus the
            # replay slot. Shown in the notice when the budget exceeds one,
            # so a user watching repeated recoveries sees the ladder
            # advancing, not looping.
            _continue_no = max(slot._empty_response_retries - 1, 1)
            _empty_rung = EMPTY_RUNG_CONTINUE
            if _empty_activity.productive or slot._empty_episode_productive:
                # Same rung, different words, because the words are read by
                # the MODEL and by the user. "returned nothing twice" is
                # false for a turn that streamed an answer or ran tools, and
                # telling a model its completed work produced no output is an
                # invitation to redo it — the side-effect duplication this
                # path exists to avoid. The split is per episode, like the
                # give-up rung's: a continuation that streamed nothing still
                # follows a turn that did work.
                slot.append(
                    "notice",
                    (
                        "ℹ️ The turn ended without a closing reply — continuing "
                        "once from what already ran."
                        if _max_continues == 1
                        else "ℹ️ The turn ended without a closing reply — "
                        "continuing from what already ran "
                        f"(recovery {_continue_no} of {_max_continues})."
                    ),
                    "msg msg-info",
                    meta={"kind": EMPTY_TURN_NOTICE_KIND},
                )
                _empty_continue_msg = _ACTIVITY_NO_REPLY_CONTINUE_MSG
            else:
                slot.append(
                    "notice",
                    (
                        "ℹ️ The model returned nothing twice — auto-continuing once."
                        if _max_continues == 1
                        else "ℹ️ The model returned nothing — auto-continuing "
                        f"(recovery {_continue_no} of {_max_continues})."
                    ),
                    "msg msg-info",
                    meta={"kind": EMPTY_TURN_NOTICE_KIND},
                )
                _empty_continue_msg = _EMPTY_AUTO_CONTINUE_MSG
            _queue_recovery(
                turn,
                0,
                _empty_continue_msg,
                kind=SYNTHETIC_RECOVERY_KIND,
                payload=RecoveryPayload.CONTINUATION,
            )
            _retrying_empty = True
            turn._retrying_empty = _retrying_empty
        else:
            _empty_rung = EMPTY_RUNG_GIVE_UP
            # Recoverable, usually-transient: surface a soft "notice" card
            # (not a red "error" card) so a self-healing event doesn't read
            # like a crash. Single emit (see the note in _on_process_died):
            # slot.append persists + broadcasts one chat_message via
            # _on_message; no explicit broadcast_ws. Same rung, different
            # words, for the same reason as the continue rung above: the
            # card is read by the MODEL (via the transcript) and by the
            # user. "returned nothing" is false for a productive turn and
            # invites a redo of work whose side effects already landed,
            # and a recovery claim is false on the paths that reach
            # give-up with the recovery counter still at zero (nested
            # depth>0 turns; a gate-off zero-counter give-up is only
            # reachable productive and takes the productive wording). The
            # counter counts budget spent, not which rungs ran (flag-off
            # reaches give-up at one with no auto-continue; a productive
            # turn's continuation reaches it at two with no verbatim
            # retry), so the non-zero clause claims only that automatic
            # recovery was attempted. The sentences are the channel driver's
            # empty-turn verdict too (``messaging.empty_turn_copy``), so a
            # channel thread mirrored into this transcript reads one story.
            if _empty_activity.productive or slot._empty_episode_productive:
                _empty_msg = EMPTY_TURN_NOTICE_AFTER_WORK
            elif slot._empty_response_retries > 0:
                _empty_msg = EMPTY_TURN_NOTICE_AFTER_RECOVERY
            else:
                _empty_msg = EMPTY_TURN_NOTICE
            slot.append("notice", _empty_msg, "msg msg-info", meta={"kind": EMPTY_TURN_NOTICE_KIND})
        # ONE warning per empty verdict, emitted AFTER the rung is chosen so
        # the log line carries the decision rather than only the symptom. The
        # predecessor logged just "Empty model response (attempt N)", which
        # could not distinguish a provider that generated nothing from a turn
        # whose answer a tool boundary flushed away, from a turn no terminal
        # event ever closed — three different faults with three different
        # owners, and the field incident hit all three in three consecutive
        # attempts. Every field here is a closed value or a bool by
        # construction (see EmptyTurnActivity): no prompt, no response, no
        # thinking, no tool arguments or results, no paths, no identities, no
        # token counts and no costs.
        logger.warning(
            "Empty model response for slot %s (attempt %d) cause=%s rung=%s "
            "stop_reason=%s terminal=%s synthetic=%s text=%s flushed_visible=%s "
            "tools=%s thinking=%s billed=%s",
            slot.key,
            _empty_attempt,
            _empty_cause,
            _empty_rung,
            _empty_activity.stop_reason,
            _empty_activity.saw_terminal,
            _empty_activity.terminal_synthetic,
            _empty_activity.saw_text,
            _empty_activity.flushed_visible,
            _empty_activity.had_tools,
            _empty_activity.had_thinking,
            _empty_activity.billed,
        )
    # Diagnostic-only drift sensor: exact matching stays the replay authority,
    # while a blocker-adjacent near miss after proven read-only Kiro work is
    # observable for future grammar decisions. Never log the model text.
    if (
        _stop_reason == STOP_REASON_END_TURN
        and not _refusal_reasons
        and tool_calls_are_read_only_preparation(
            _turn_tool_calls,
            tuple(_turn_tool_identities),
            frozenset(_turn_successful_tool_call_ids),
            builtin_identity_trusted=_builtin_identity_trusted,
        )
        and is_false_current_tool_blocker_near_miss(turn.assistant_text)
    ):
        logger.warning(
            "False tool-blocker wording drift after %d proven read-only call(s) "
            "for slot %s; replay skipped",
            _turn_tool_calls,
            slot.key,
        )
