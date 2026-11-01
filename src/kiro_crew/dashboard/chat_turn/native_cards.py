"""Harness-native sub-agent Activity cards (kiro-cli's, KAS's and Claude Code's) and the native
AskUserQuestion card a dashboard turn shows, and the stream's native sub-agent arms
(``_on_subagent_list``, ``_on_subagent_activity``)."""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.native_subagent_cards import (
    _NATIVE_RUNNING_PREFIX,
    NATIVE_SUBAGENT_INPUT_CHARS,
    NATIVE_SUBAGENT_RESULT_CHARS,
    NATIVE_SUBAGENT_UNNAMED_FEED_LINE,
    _admit_native_call,
    _native_output_is_new,
)
from kiro_crew.dashboard.state import (
    NATIVE_SUBAGENT_CALLS_OVERFLOW_FEED_LINE,
    NATIVE_SUBAGENT_OUTPUT_HARD,
    NATIVE_SUBAGENT_OUTPUT_TAIL,
    NATIVE_SUBAGENT_TERMINAL_KEEP,
    NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
)
from kiro_crew.security import StreamRedactor, redact, redact_and_truncate
from kiro_crew.validation import ValidationError, validate_ask_user_question

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        NATIVE_SUBAGENT_DONE_RESULT_CAP,
        NATIVE_SUBAGENT_DONE_TRUNC_MARKER,
        STOP_REASON_CANCELLED,
        SUBAGENT_ACTIVITY_THINKING,
        SUBAGENT_ACTIVITY_TOOL_RESULT,
        TERMINAL_TOOL_STATUSES,
        _redact_tool_field,
        logger,
        redact_credentials,
        redact_exfiltration_urls,
        session_directive,
    )
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn

# Native subagent cards carry a short error string only, so a long provider
# message is clipped. The request id is what identifies the failure
# server-side and the formatter appends it LAST, so a plain head-slice drops
# precisely the part worth keeping.
_MAX_NATIVE_CARD_ERROR = 200


_RE_TRAILING_REQUEST_ID = re.compile(r"\(request_id:\s*[0-9a-fA-F-]+\)\s*$")


def _clip_card_error(text: str, limit: int = _MAX_NATIVE_CARD_ERROR) -> str:
    """Clip *text* to *limit* characters, keeping any trailing request id."""
    if len(text) <= limit:
        return text
    match = _RE_TRAILING_REQUEST_ID.search(text)
    if not match:
        return text[:limit]
    suffix = match.group(0).strip()
    head = limit - len(suffix) - 4  # room for the elision marker and a space
    if head <= 0:
        return text[:limit]
    return f"{text[:head]}... {suffix}"


async def _post_native_question_card(state: Any, slot_key: str, tool_input: str) -> bool:
    """Post kiro-cli's native ``AskUserQuestion`` card through the server owner.

    The card goes through the SAME path the MCP ``ask_question`` directive uses
    (``DashboardState.post_question_card``): one minted ``card_id`` on the
    frame, one ``_question_pending`` record on the slot, one redaction pass. A
    live user row or a consumed steer then retires the record and broadcasts
    ``question_card_resolved`` exactly as for an MCP card, so the client keeps
    no lifecycle of its own for this kind.

    Schema validation stays here (``validate_ask_user_question`` is the
    tool-input contract); text redaction lives in the coordinator, which also
    rejects questions that collapse to the same text after redaction.

    Returns False -- and logs -- when the input is unusable. Nothing raised here
    may reach the turn: a malformed native card is a warning, not a turn error.
    """
    try:
        questions = validate_ask_user_question(json.loads(tool_input))
        await state.post_question_card(slot_key, questions, native=True)
    except (
        # ``json.JSONDecodeError`` and the coordinator's redaction-collision
        # refusal are both ValueError.
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        ValidationError,
    ) as exc:
        logger.warning("AskUserQuestion validation failed: %s", exc)
        return False
    return True


# Native kiro-cli subagents (``use_subagent``) are surfaced in the Activity tab
# via the ``_kiro.dev/subagent/list_update`` notification (one card per
# sub-agent), handled by ``_native_subagent_sync`` below. The list_update gives
# authoritative per-sub-agent identity/status, so the ``subagent`` tool call's
# ``stages`` payload is not parsed.

_NATIVE_SUBAGENT_STALE_SECS = 120.0  # auto-close cards with no progress after 2 min


def _native_done_result(chunks: "list[str] | None") -> str:
    """Return the newest bounded native-card output with a truncation marker."""
    joined = "".join(chunks or [])
    if len(joined) <= NATIVE_SUBAGENT_DONE_RESULT_CAP:
        return joined
    return NATIVE_SUBAGENT_DONE_TRUNC_MARKER + joined[-NATIVE_SUBAGENT_DONE_RESULT_CAP:]


def _append_native_output(
    buf: list[str],
    text: str,
    total: int,
    cap: int = NATIVE_SUBAGENT_OUTPUT_TAIL,
    hard: int = NATIVE_SUBAGENT_OUTPUT_HARD,
) -> int:
    """Append output and collapse it to the newest tail past the hard ceiling."""
    buf.append(text)
    total += len(text)
    if total > hard:
        tail = "".join(buf)[-cap:]
        buf[:] = [tail]
        total = len(tail)
    return total


_NATIVE_NOT_APPLIED_NOTE = (
    "\n\n[Not applied: a session-bound tool called from a sub-agent has no session to act on.]"
)


def _native_unapplied_directive_text(text: str) -> str:
    """A call's output as the card shows it when the call was a session-bound
    directive: the marker gone and the not-applied note in its place.

    A directive tool answers with a marker the runner's gate reads to apply it
    to the parent session. A child's directive is never applied -- the gate
    refuses the flat copy of its call, and the child-tagged frame never reaches
    the gate at all -- so the marker is control text with nothing behind it,
    and the confirmation beside it describes an effect that did not happen.
    The runner says so on the parent's tool row; the card says the same.
    """
    if session_directive.has_marker(text) or session_directive.is_refusal(text):
        return session_directive.strip_marker(text) + _NATIVE_NOT_APPLIED_NOTE
    return text


def _native_result_feed_line(info: dict, tcid: str, out: str) -> str:
    """The feed's entry for a call's output: under the call's title (with
    kiro-cli's ``Running: `` stripped), under the unnamed line when the bound
    kept no title for the call, or bare for a kept slot no frame ever titled."""
    titles = info.get("call_titles") or {}
    if tcid not in titles:
        return f"{NATIVE_SUBAGENT_UNNAMED_FEED_LINE}{out}\n"
    if titles[tcid]:
        return f"\u2192 {_NATIVE_RUNNING_PREFIX.sub('', str(titles[tcid]))}\n{out}\n"
    return f"{out}\n"


def _native_card_feed(card_output, card_id: str, info: "dict | None" = None) -> str:
    """Build, bound, and redact native output at the broadcast boundary.

    A card whose run crossed the call bound says so once, on the result's last
    line, as its running snapshots do on their tail
    (``DashboardState.native_subagent_snapshots``): the outputs filed under the
    unnamed line otherwise read like calls that were never named. The count of
    refused frames stays on the record (``calls_refused``).
    """
    chunks = list((card_output or {}).get(card_id) or [])
    if info and info.get("calls_refused"):
        chunks.append(NATIVE_SUBAGENT_CALLS_OVERFLOW_FEED_LINE)
    feed = _native_done_result(chunks)
    if feed:
        feed, _ = redact_exfiltration_urls(feed)
        feed, _ = redact_credentials(feed)
    return feed


def _redact_native_text(text: str) -> str:
    """The two redactions every native-card string takes before the wire."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _native_streams(streams: "dict[str, dict[str, StreamRedactor]]", sid: str) -> dict:
    """The child's rolling redactors: one per stream the card relays, plus one
    per admitted call whose output is arriving in fragments (``call:<id>``).

    kiro-cli streams a child's reasoning and text in small chunks and the
    client joins them back together, so a credential split across two chunks
    would cross the wire whole if each chunk were redacted on its own. The
    rolling redactor withholds a chunk's trailing credential-class run until a
    terminator or the segment's end -- the same protection the parent's own
    ``chat_thinking`` and ``chat_chunk`` streams get. A call's fragments get
    the same treatment on the one path that publishes them (see the result
    arm of ``_native_subagent_activity``): a bearer header whose prefix and
    token arrive in separate frames is a credential only when read together.
    """
    return streams.setdefault(sid, {"thinking": StreamRedactor(), "text": StreamRedactor()})


def _native_call_redactor_key(tcid: str) -> str:
    return f"call:{tcid}"


def _native_flush_pending_call(red: "dict | None", safe: str, tcid: str) -> str:
    """The text a call's fallback publishes: its committed-safe fragments plus
    whatever its rolling redactor still withholds, redacted whole."""
    redactor = red.pop(_native_call_redactor_key(tcid), None) if red else None
    return safe + (redactor.flush() if redactor is not None else "")


def _flush_native_streams(
    state,
    slot,
    card_id: str,
    red: "dict[str, StreamRedactor]",
    card_output: "dict[str, list[str]]",
    card_output_len: "dict[str, int]",
    *,
    thinking: bool,
    text: bool,
) -> None:
    """Emit what the child's rolling redactors still withhold.

    A segment ends when the child's own output moves on -- text after
    reasoning, reasoning after text, a new tool call -- or when the child ends.
    A tool's result or a title refinement is the host reporting on a call, not
    the child speaking, and ends no segment: flushing on one would hand a
    credential split across the chunks either side of it to the wire in two
    halves that neither redactor could see whole. The text tail joins the
    card's feed like any other text chunk.
    """
    if thinking:
        tail = red["thinking"].flush()
        if tail:
            state.broadcast_ws("subagent_thinking", {"id": card_id, "slot": slot.key, "text": tail})
    if text:
        tail = red["text"].flush()
        if tail:
            card_output_len[card_id] = _append_native_output(
                card_output.setdefault(card_id, []),
                tail,
                card_output_len.get(card_id, 0),
            )
            state.broadcast_ws("subagent_chunk", {"id": card_id, "slot": slot.key, "text": tail})


def _close_native_card_streams(
    state,
    slot,
    card_id: str,
    sid: str,
    info: dict,
    card_output: "dict[str, list[str]] | None",
    card_output_len: "dict[str, int] | None",
    streams: "dict[str, dict[str, StreamRedactor]] | None",
) -> None:
    """At a child's end, emit its withheld tails and drop its transients.

    Runs before the done-time feed is built, so the text tail is part of the
    card's ``result``, and so are the pending fragments of any call that never
    reached its terminal frame: they print here, in order and redacted whole
    through the call's own rolling redactor, under the call's title.
    The call-title map, the pending fragments and the redactors are per-run
    working state: the terminal record kept for reconnect replay carries only
    the bounded fields the snapshot reads.
    """
    red = streams.pop(sid, None) if streams is not None else None
    if red is not None:
        _flush_native_streams(
            state,
            slot,
            card_id,
            red,
            card_output if card_output is not None else {},
            card_output_len if card_output_len is not None else {},
            thinking=True,
            text=True,
        )
    for tcid, safe in (info.pop("call_pending", None) or {}).items():
        text = _native_flush_pending_call(red, safe, tcid)
        if not text:
            continue
        out = _redact_native_text(_native_unapplied_directive_text(text))[
            :NATIVE_SUBAGENT_RESULT_CHARS
        ]
        if card_output is not None and card_output_len is not None:
            card_output_len[card_id] = _append_native_output(
                card_output.setdefault(card_id, []),
                _native_result_feed_line(info, tcid, out),
                card_output_len.get(card_id, 0),
            )
        state.broadcast_ws(
            "subagent_tool_result",
            {"id": card_id, "slot": slot.key, "tool_call_id": tcid, "output": out},
        )
    info.pop("call_titles", None)


def _native_project_result(
    state,
    slot,
    event,
    card_id: str,
    info: dict,
    red: "dict[str, StreamRedactor]",
    card_output: "dict[str, list[str]]",
    card_output_len: "dict[str, int]",
    tc_card: "dict[str, str]",
    result_seen: "dict[str, int]",
) -> None:
    """Project one result frame of an attributed call onto its card.

    Both routes a result can take land here: the child-tagged frame
    (``_native_subagent_activity``) and a parent-tagged copy of it on a wire
    that copies a child's calls onto the parent stream
    (``_native_subagent_flat_result``). The terminal rule and the per-call
    rolling redactor below are what keep a credential split across two
    fragments off the wire, so no route may publish a fragment around them.
    A directive marker in the text is a session-bound call of the child's,
    which is never applied: the card shows it as the runner does the parent's
    row, marker gone and the not-applied note in its place.
    """
    tcid = event.tool_call_id
    if not tcid:
        return
    payload: dict = {"id": card_id, "slot": slot.key, "tool_call_id": tcid}
    status = event.tool_status or ("completed" if event.tool_final else "")
    if status:
        payload["status"] = status
    terminal = bool(event.tool_final) or status in TERMINAL_TOOL_STATUSES
    # A call's output prints from its TERMINAL frame, which carries the
    # whole of it. A frame in progress carries its status alone: a
    # fragment broadcast on its own is redacted on its own, and a
    # credential split across two fragments would cross the wire in
    # halves that neither redaction sees. The fragments are kept -- for an
    # admitted call only, in order, through one rolling redactor per call
    # whose committed output (never ending inside a credential-class run)
    # accumulates here, bounded like the output it stands in for -- and
    # print if the terminal frame carries no text, or at the child's close
    # (``_close_native_card_streams``) if no terminal frame comes. A
    # refused call holds no row and keeps nothing.
    pending = info.setdefault("call_pending", {}) if tcid in tc_card else None
    text = event.tool_output
    if not terminal:
        if text and pending is not None:
            redactor = red.setdefault(_native_call_redactor_key(tcid), StreamRedactor())
            pending[tcid] = (pending.get(tcid, "") + redactor.feed(text))[
                :NATIVE_SUBAGENT_RESULT_CHARS
            ]
        if not status:
            return
        state.broadcast_ws("subagent_tool_result", payload)
        return
    if pending is not None:
        kept = pending.pop(tcid, None)
        if kept is not None or _native_call_redactor_key(tcid) in red:
            kept = _native_flush_pending_call(red, kept or "", tcid)
        text = text or kept
    if text and _native_output_is_new(result_seen, tc_card, tcid, text):
        out = _redact_native_text(_native_unapplied_directive_text(text))[
            :NATIVE_SUBAGENT_RESULT_CHARS
        ]
        payload["output"] = out
        card_output_len[card_id] = _append_native_output(
            card_output.setdefault(card_id, []),
            _native_result_feed_line(info, tcid, out),
            card_output_len.get(card_id, 0),
        )
    elif not status:
        return
    state.broadcast_ws("subagent_tool_result", payload)
    return


def _native_subagent_flat_call(
    state,
    slot,
    event,
    tracker: "dict[str, dict]",
    tc_card: "dict[str, str]",
) -> None:
    """Project a PARENT-tagged ``tool_call`` of a child's call onto the card.

    A wire that copies a child's calls onto the parent stream announces each
    call twice: the child channel's activity frame admits it under the bare
    tool name, and the parent-tagged copy carries the command-bearing title.
    The copy is a REFINEMENT of the admitted call, not a second call: it
    refines the kept title slot (so the result row files the output under
    the full name) and re-sends the ``subagent_tool`` frame the client keyed
    on the id, with the count where admission left it. It writes nothing to
    the feed -- the result row names the call there, once; a title line of
    its own above it named the call twice.
    """
    tcid = event.tool_call_id
    card_id = tc_card.get(tcid) if tcid else None
    if not card_id:
        return
    for sid, info in tracker.items():
        if str(info.get("id") or f"native:{_redact_tool_field(sid)}") == card_id:
            break
    else:
        return
    if info.get("done"):
        return
    title = _redact_native_text(event.title or "")[:80]
    titles = info.get("call_titles") or {}
    if tcid in titles:
        if title:
            titles[tcid] = title
        else:
            title = titles[tcid]
    if title:
        info["last_tool"] = title
    payload = {
        "id": card_id,
        "slot": slot.key,
        "tool": title,
        "tool_call_id": tcid,
        "tool_count": int(info.get("tool_count") or 0),
    }
    if event.tool_input:
        payload["input"] = _redact_native_text(event.tool_input)[:NATIVE_SUBAGENT_INPUT_CHARS]
    state.broadcast_ws("subagent_tool", payload)


def _native_subagent_flat_result(
    state,
    slot,
    event,
    tracker: "dict[str, dict]",
    card_output: "dict[str, list[str]]",
    card_output_len: "dict[str, int]",
    tc_card: "dict[str, str]",
    result_seen: "dict[str, int]",
    streams: "dict[str, dict[str, StreamRedactor]]",
) -> None:
    """Project a PARENT-tagged result frame of a child's call onto the card.

    A wire that copies a child's calls onto the parent stream delivers the
    child's results as parent-tagged ``EVENT_TOOL_RESULT`` frames whose id the
    activity path has attributed (``tc_card``). They take the same terminal
    rule and per-call rolling redactor as the child-tagged frame: redacting
    each fragment alone and publishing it would send a credential split across
    two fragments over the wire in halves that neither redaction sees.
    ``result_seen`` is shared with the activity path, so a result both routes
    deliver prints once.
    """
    tcid = event.tool_call_id
    card_id = tc_card.get(tcid) if tcid else None
    if not card_id:
        return
    for sid, info in tracker.items():
        if str(info.get("id") or f"native:{_redact_tool_field(sid)}") == card_id:
            break
    else:
        return
    if info.get("done"):
        return
    _native_project_result(
        state,
        slot,
        event,
        card_id,
        info,
        _native_streams(streams, sid),
        card_output,
        card_output_len,
        tc_card,
        result_seen,
    )


def _native_subagent_activity(
    state,
    slot,
    event,
    tracker: "dict[str, dict]",
    card_output: "dict[str, list[str]]",
    card_output_len: "dict[str, int]",
    tc_card: "dict[str, str]",
    result_seen: "dict[str, int]",
    streams: "dict[str, dict[str, StreamRedactor]]",
) -> None:
    """Project one child-routed ``EVENT_SUBAGENT_ACTIVITY`` onto its native card.

    kiro-cli runs a native ``subagent`` crew inside the parent process and
    emits every child frame -- its tool calls, their results, its reasoning and
    its text -- tagged with the CHILD's session id. The handle re-tags each as
    subagent activity rather than parent transcript (a child's tool pill must
    not render as the parent's), so this projection is the only path by which
    the child's work reaches its Subagents-panel card. Without it a child that
    only calls tools reads "Starting…" with "Waiting for output…" for its whole
    run, however long: the roster frames that spawn and close the card say
    nothing about what happens in between.

    Four shapes, told apart by the event's ``activity`` tag and fields:

    * a tool call (``tool_call_id`` + ``title``): a ``subagent_tool`` frame
      naming the call, so the card flips to "Running Tool" and lists it. A
      refinement for the same id (kiro-cli sends the bare tool name first and
      the command-bearing title after) rides the same frame and refines the
      row the client keyed on the id; the count moves once per call. Its
      first frame admits the call (``_admit_native_call``) into every store
      keyed on its id -- the attribution map, its card's titles, the record
      of outputs already printed -- under one bound on entries and one on
      id length (``NATIVE_SUBAGENT_CALLS_MAX``,
      ``NATIVE_SUBAGENT_CALL_ID_CHARS``); a call refused past either still
      streams and counts but holds no row, and the feed files its output
      under ``NATIVE_SUBAGENT_UNNAMED_FEED_LINE``.
    * a tool result (``SUBAGENT_ACTIVITY_TOOL_RESULT``): a
      ``subagent_tool_result`` frame with the parser's redacted, bounded
      output, which also joins the card's feed (the done-time ``result``)
      under the call's title. The output prints from the call's TERMINAL
      frame; a frame in progress carries its status alone and its text is
      held as the admitted call's pending fragment (``_native_project_result``
      has the rule). A printed output is told from a repeat by content
      digest per call (``_native_output_is_new``), so a later, different
      output of the same call prints too. A
      result is the first frame seen of a call whose opening frame predates
      the child's admission (``_admit_native_call``): the call is counted
      and slotted here, and its row is titled by the refinement that follows.
    * reasoning (``SUBAGENT_ACTIVITY_THINKING``): a ``subagent_thinking``
      frame, kept out of the feed -- the feed is the result the parent reads.
    * streamed text: a ``subagent_chunk`` frame, as before, into the feed.

    Every string is redacted at this boundary. Reasoning and text are streams,
    so they go through the child's rolling redactors (``streams``): a chunk's
    trailing credential-class run is withheld until a terminator, the child's
    next output of another kind (text after reasoning, reasoning after text, a
    new tool call), or the child's end, so a credential split across two
    chunks never crosses the wire whole. A tool result or a title refinement
    arriving between the two chunks is the host's frame, not the child's, and
    does not end the segment. ``tc_card`` is still filled so the
    flat-frame paths keep attributing a parent-tagged child frame (an older
    kiro-cli wire) to the card; ``result_seen`` is shared with them so neither
    path prints an output the other already did.
    """
    sid = event.sub_session_id
    info = tracker.get(sid)
    if not info:
        return
    card_id = str(info.get("id") or f"native:{_redact_tool_field(sid)}")
    activity = getattr(event, "activity", "")
    tcid = event.tool_call_id
    first = bool(tcid) and tcid not in tc_card
    title = _redact_native_text(event.title or "")[:80] if tcid else ""
    admitted = False
    if first:
        # Admitted BEFORE the projection is gated on the record: the flat
        # tool_call/tool_result arms read ``tc_card`` to file a child's copied
        # call under its row. (A child's session-bound directive is refused by
        # ``_native_child_tc``, which ``_on_subagent_activity`` fills before
        # this admission, whether or not the call holds a row.) A child the
        # roster already closed, or one admitted with an empty task (which the
        # sync marks done at once and never cards), still makes calls and still
        # gets no card -- and a call of its the bound refuses is still counted
        # on its record. A call first seen through its result is admitted
        # untitled; its refinement names it.
        admitted = _admit_native_call(
            info,
            tc_card,
            card_id,
            tcid,
            "" if activity == SUBAGENT_ACTIVITY_TOOL_RESULT else title,
        )
    if info.get("done"):
        return
    red = _native_streams(streams, sid)
    # Only the child's own output ends a segment: the other stream's chunk, or
    # a NEW call (its first frame). A result or a refinement of a known call is
    # the host's frame about that call -- the child may still be mid-thought
    # around it, so neither redactor flushes on it. A call first seen through
    # its result is admitted below but ends no segment either: the result is
    # still the host's frame, about a call made before the chunks the
    # redactors hold. Only an ADMITTED new call counts: a call the bound
    # refused holds no row, so every one of its frames -- the first and each
    # refinement -- looks like a first frame, and flushing on one would send a
    # withheld credential head over the wire with its tail following bare.
    new_call = admitted and activity != SUBAGENT_ACTIVITY_TOOL_RESULT
    is_text = not activity and not tcid and bool(event.text)
    _flush_native_streams(
        state,
        slot,
        card_id,
        red,
        card_output,
        card_output_len,
        thinking=is_text or new_call,
        text=activity == SUBAGENT_ACTIVITY_THINKING or new_call,
    )
    if activity == SUBAGENT_ACTIVITY_THINKING:
        wire = red["thinking"].feed(event.text or "")
        if wire:
            state.broadcast_ws("subagent_thinking", {"id": card_id, "slot": slot.key, "text": wire})
        return
    if activity == SUBAGENT_ACTIVITY_TOOL_RESULT:
        _native_project_result(
            state,
            slot,
            event,
            card_id,
            info,
            red,
            card_output,
            card_output_len,
            tc_card,
            result_seen,
        )
        return
    if tcid:
        if not first:
            titles = info.get("call_titles") or {}
            if tcid in titles:
                # A refinement of a kept call refines its slot (a call first
                # seen through its result, or announced untitled, is titled
                # here); an untitled frame reads the title back for the wire.
                if title:
                    titles[tcid] = title
                else:
                    title = titles[tcid]
        if title:
            info["last_tool"] = title
        payload = {
            "id": card_id,
            "slot": slot.key,
            "tool": title,
            "tool_call_id": tcid,
            # Admitted calls only move the count; a call past the bound still
            # streams with the count where the bound left it.
            "tool_count": int(info.get("tool_count") or 0),
        }
        if event.tool_input:
            payload["input"] = _redact_native_text(event.tool_input)[:NATIVE_SUBAGENT_INPUT_CHARS]
        state.broadcast_ws("subagent_tool", payload)
        return
    if event.text:
        wire = red["text"].feed(event.text)
        if not wire:
            return
        card_output_len[card_id] = _append_native_output(
            card_output.setdefault(card_id, []),
            wire,
            card_output_len.get(card_id, 0),
        )
        state.broadcast_ws("subagent_chunk", {"id": card_id, "slot": slot.key, "text": wire})


def _native_subagent_sync(
    state, slot, subagents, tracker, card_output=None, card_output_len=None, streams=None
) -> None:
    """Reconcile per-subagent Activity cards from a kiro-cli
    ``_kiro.dev/subagent/list_update`` notification.

    Native (``use_subagent``) crews run *inside* the parent kiro-cli session, so
    their internal tool calls are not attributable per sub-agent over standard
    ACP. But kiro-cli also emits this list (the same data its TUI shows) with one
    entry per sub-agent carrying ``sessionId``, ``sessionName``,
    ``role``/``agentName``, ``initialQuery`` and ``status``. We map each entry to
    its own Activity card (``native:<sessionId>``): spawned when first seen,
    completed when its status terminates. This yields one card per sub-agent
    (matching the spawn_run/spawn_sub_agents card model) with no agent-prompt
    changes.

    ``tracker`` is per-turn state: ``{session_id: {started, done, agent, task}}``.
    """
    if not isinstance(subagents, list):
        return
    _running = ("working", "running", "pending", "queued", "in_progress", "")
    now = time.time()
    # Track which sids are still reported by kiro-cli this update
    _seen_sids: set[str] = set()
    for sub in subagents:
        if not isinstance(sub, dict):
            continue
        sid = str(sub.get("sessionId") or "")
        if not sid:
            continue
        _seen_sids.add(sid)
        card_id = f"native:{_redact_tool_field(sid)}"
        _status_raw = sub.get("status")
        status = _status_raw if isinstance(_status_raw, dict) else {}
        stype = str(status.get("type") or "").lower()
        smsg = str(status.get("message") or "")
        if sid not in tracker:
            agent, _ = redact_exfiltration_urls(str(sub.get("role") or sub.get("agentName") or ""))
            agent, _ = redact_credentials(agent)
            task = redact_and_truncate(
                str(sub.get("initialQuery") or sub.get("sessionName") or ""), 2000
            )
            # Skip cards with empty task entirely — kiro-cli sometimes emits
            # list_update notifications where initialQuery/sessionName are both
            # empty. Showing an Activity card with no meaningful input is
            # confusing UX ("Starting..." with nothing to explain what it does).
            # Mark as done immediately so we don't re-process on next update.
            if not task.strip():
                tracker[sid] = {
                    "started": now,
                    "done": True,
                    "agent": agent,
                    "task": "",
                    "last_activity": now,
                }
                logger.debug(
                    "native subagent skipped (empty task): sid=%s slot=%s",
                    sid,
                    slot.key,
                )
                continue
            tracker[sid] = {
                "id": card_id,
                "started": now,
                "done": False,
                "agent": agent,
                "task": task,
                "last_activity": now,
                "last_tool": "",
            }
            # Register in state-level dict so DELETE /api/spawn/{id} can cancel native cards.
            _register_native_card(state, card_id, slot.key, sid)
            logger.debug(
                "native subagent spawn broadcast: id=%s agent=%s slot=%s",
                card_id,
                agent,
                slot.key,
            )
            state.broadcast_ws(
                "subagent_spawn",
                {"id": card_id, "slot": slot.key, "task": task, "agent": agent},
            )
        else:
            # Card is still being reported this update — keep it alive. Update
            # unconditionally (not just on non-empty smsg): a card reported
            # with empty status messages for >120s would otherwise be
            # auto-closed the instant it disappears from the list, since its
            # last_activity would still be the creation timestamp.
            tracker[sid]["last_activity"] = now
        info = tracker[sid]
        if info["done"]:
            continue
        if stype and stype not in _running:
            err = None
            if stype in ("failed", "error") and smsg:
                err, _ = redact_exfiltration_urls(smsg)
                err, _ = redact_credentials(err)
                err = _clip_card_error(err)
            info["done"] = True
            _close_native_card_streams(
                state, slot, card_id, sid, info, card_output, card_output_len, streams
            )
            _feed = _native_card_feed(card_output, card_id, info)
            _elapsed = time.time() - info["started"]
            _result = _feed or "(output in chat)"
            info["elapsed"] = _elapsed
            info["error"] = err
            info["result"] = _result
            info["done_at"] = time.time()
            _unregister_native_card(state, card_id)
            state.broadcast_ws(
                "subagent_done",
                {
                    "id": card_id,
                    "slot": slot.key,
                    "elapsed": _elapsed,
                    "error": err,
                    "task": info["task"],
                    "agent": info["agent"],
                    "result": _result,
                },
            )
        elif smsg and smsg.lower() != "running":
            # Surface a non-generic status message as the card's current tool.
            tool, _ = redact_exfiltration_urls(smsg)
            tool, _ = redact_credentials(tool)
            info["last_tool"] = tool[:80]
            state.broadcast_ws(
                "subagent_tool",
                {"id": card_id, "slot": slot.key, "tool": tool[:80]},
            )

    # Staleness timeout: auto-close cards that kiro-cli has stopped reporting and
    # that have had no activity for too long. This prevents native sub-agent
    # cards from staying stuck in "Starting..." indefinitely when kiro-cli
    # fails to emit a terminal status. Cards still present in the current
    # list_update are alive by definition and never timed out here.
    for sid, info in list(tracker.items()):
        if info.get("done"):
            continue
        if sid in _seen_sids:
            continue  # still reported by kiro-cli this update — not stale
        last_act = info.get("last_activity", info["started"])
        if now - last_act > _NATIVE_SUBAGENT_STALE_SECS:
            info["done"] = True
            _cid = f"native:{_redact_tool_field(sid)}"
            _close_native_card_streams(
                state, slot, _cid, sid, info, card_output, card_output_len, streams
            )
            _feed = _native_card_feed(card_output, _cid, info)
            _elapsed = now - info["started"]
            _error = "timed out (no activity)"
            _result = _feed or "(no output received)"
            info["elapsed"] = _elapsed
            info["error"] = _error
            info["result"] = _result
            info["done_at"] = now
            _unregister_native_card(state, _cid)
            state.broadcast_ws(
                "subagent_done",
                {
                    "id": _cid,
                    "slot": slot.key,
                    "elapsed": _elapsed,
                    "error": _error,
                    "task": info.get("task", ""),
                    "agent": info.get("agent", ""),
                    "result": _result,
                },
            )
            logger.info(
                "native subagent %s auto-closed: stale for %.0fs",
                _cid,
                now - last_act,
            )


def _register_native_card(state, card_id: str, slot_key: str, session_id: str) -> None:
    """Register a native subagent card in the state-level dict for cancel support."""
    if not hasattr(state, "_native_cards"):
        state._native_cards = {}  # card_id -> {slot, session_id, started}
    state._native_cards[card_id] = {
        "slot": slot_key,
        "session_id": session_id,
        "started": time.time(),
    }


def _unregister_native_card(state, card_id: str) -> None:
    """Remove a native subagent card from the state-level dict."""
    if hasattr(state, "_native_cards"):
        state._native_cards.pop(card_id, None)


NATIVE_SUBAGENT_INTERRUPTED_ERROR = "interrupted (parent turn ended)"


def _native_subagent_close_all(
    state,
    slot,
    tracker,
    card_output=None,
    error: str | None = None,
    *,
    card_output_len: "dict[str, int] | None",
    streams: "dict[str, dict[str, StreamRedactor]] | None",
) -> None:
    """Complete any still-open native subagent cards (turn-end safety net).

    Reached when the parent turn ends -- a user cancel, a turn failure, a
    missed terminal roster frame -- while a child may still be mid-stream.
    Each child's withheld redactor tails are flushed first, the same way the
    roster-driven and staleness closes do, so the text a child had already
    produced reaches its card and terminal ``result`` instead of being lost
    with the per-run stream state. The two maps are keyword-only and required
    so a call site cannot silently drop back to the lossy shape.

    A record already done (the dashboard's stop path marks it without going
    through this module) only sheds its per-run transients here, with nothing
    emitted: the card is closed, and the terminal record kept for replay must
    carry only the bounded fields the snapshot reads.

    ``error`` is None only when the parent turn landed normally; a stopped,
    timed-out or crashed turn passes ``NATIVE_SUBAGENT_INTERRUPTED_ERROR`` so a
    child cut off mid-run does not show as a clean completion.
    """
    for sid, info in tracker.items():
        _cid = f"native:{_redact_tool_field(sid)}"
        if info.get("done"):
            if streams is not None:
                streams.pop(sid, None)
            info.pop("call_titles", None)
            info.pop("call_pending", None)
            continue
        info["done"] = True
        _close_native_card_streams(
            state, slot, _cid, sid, info, card_output, card_output_len, streams
        )
        _feed = _native_card_feed(card_output, _cid, info)
        _elapsed = time.time() - info.get("started", time.time())
        _result = _feed or "(output in chat)"
        info["elapsed"] = _elapsed
        info["error"] = error
        info["result"] = _result
        info["done_at"] = time.time()
        _unregister_native_card(state, _cid)
        state.broadcast_ws(
            "subagent_done",
            {
                "id": _cid,
                "slot": slot.key,
                "elapsed": _elapsed,
                "error": error,
                "task": info.get("task", ""),
                "agent": info.get("agent", ""),
                "result": _result,
            },
        )


def _retain_terminal_native(
    tracker: "dict[str, dict]",
    keep: int = NATIVE_SUBAGENT_TERMINAL_KEEP,
    ttl_secs: float = NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
    now: "float | None" = None,
) -> "dict[str, dict]":
    """Retain recent terminal records for bounded post-turn reconnect replay."""
    current = time.time() if now is None else now
    terminal = [
        (sid, info)
        for sid, info in tracker.items()
        if info.get("done")
        and info.get("id")
        and (current - float(info.get("done_at") or 0.0)) <= ttl_secs
    ]
    if keep >= 0 and len(terminal) > keep:
        terminal.sort(key=lambda item: float(item[1].get("done_at") or 0.0), reverse=True)
        terminal = terminal[:keep]
    return {sid: info for sid, info in terminal}


def _safe_native_crew_debug_title(title: str) -> str:
    """Redact credentials/exfiltration URLs from an LLM-controlled native-crew
    tool title before it is logged. Control chars are escaped at the log call
    via %r."""
    safe, _ = redact_exfiltration_urls(title or "")
    safe, _ = redact_credentials(safe)
    return safe


async def _on_subagent_list(turn: _ChatTurn, event) -> None:
    """The session's native sub-agent list."""
    _native_card_output = turn._native_card_output
    _native_tracker = turn._native_tracker
    slot = turn.slot
    state = turn.state
    # kiro-cli per-subagent state (native use_subagent crews).
    # Reconcile one Activity card per sub-agent (spawn/done).
    logger.debug(
        "EVENT_SUBAGENT_LIST: %s subagents, slot=%s",
        len(event.subagents or []),
        slot.key,
    )
    _native_subagent_sync(
        state,
        slot,
        event.subagents,
        _native_tracker,
        _native_card_output,
        turn._native_card_output_len,
        turn._native_streams_red,
    )


async def _on_subagent_activity(turn: _ChatTurn, event) -> None:
    """A native sub-agent's activity."""
    _native_tracker = turn._native_tracker
    slot = turn.slot
    # A native child's frame, re-tagged by the handle with the child's
    # sessionId. Its tool calls, results, reasoning and text are projected
    # onto the child's card by ``_native_subagent_activity`` -- which also
    # fills the toolCallId->card map the flat tool_call/tool_result arms read,
    # for a wire that tags a child's frames with the parent's sessionId.
    _sid = event.sub_session_id
    if _sid and event.tool_call_id:
        # A child's call whether or not it has a card: the directive refusal
        # reads this set, as the messaging driver's reads its own.
        turn._native_child_tc.add(event.tool_call_id)
    # Permission-rejection notices (the handle's own "⛔ …" lines,
    # e.g. drain-time rejects yielded at turn start) arrive BEFORE
    # any subagent_list populates the per-turn tracker — dropping
    # them here would leave the user watching a child tool fail
    # with no explanation. When the card cannot exist yet, persist
    # the explanation as a slot notice instead.
    if _sid not in _native_tracker and event.text and event.text.startswith("⛔"):
        _txt = redact(event.text)
        slot.append("notice", _txt, "msg msg-info")
    _native_subagent_activity(
        turn.state,
        slot,
        event,
        _native_tracker,
        turn._native_card_output,
        turn._native_card_output_len,
        turn._native_tc_card,
        turn._native_result_seen,
        turn._native_streams_red,
    )


def _native_card_text(turn: _ChatTurn, sid: str, text: str) -> None:
    """Append a sub-agent's *text* to its open card's feed and stream it to the card."""
    _native_card_output = turn._native_card_output
    _native_card_output_len = turn._native_card_output_len
    _card_id = f"native:{_redact_tool_field(sid)}"
    # The child's own text ends both of its held segments: what its rolling
    # redactors still withhold lands first, so the feed keeps the wire's order.
    _flush_native_streams(
        turn.state,
        turn.slot,
        _card_id,
        _native_streams(turn._native_streams_red, sid),
        _native_card_output,
        _native_card_output_len,
        thinking=True,
        text=True,
    )
    _txt = redact(text)
    _native_card_output_len[_card_id] = _append_native_output(
        _native_card_output.setdefault(_card_id, []),
        _txt,
        _native_card_output_len.get(_card_id, 0),
    )
    turn.state.broadcast_ws(
        "subagent_chunk",
        {"id": _card_id, "slot": turn.slot.key, "text": _txt},
    )


def _native_flush_cut_short(turn: _ChatTurn) -> None:
    """Put the text a Claude child's redactor still holds on its card, for a turn cut short.

    A turn cancelled (Stop, the timeout ceiling, a closed tab) or ended with no
    completion (a provider error) while a card is still open gets no roster that
    would flush a child's tail, and its release closes the cards next. A turn that
    ends normally keeps the text held: a background child keeps running into the
    next turn, where its redactor still needs it. So does a turn that never opened
    a card. Only claude's ``AcpClient`` holds child text; another client either
    lacks the method or holds nothing. It never suspends, as the release phase that
    calls it must not.
    """
    if getattr(turn, "_stop_reason", "") not in ("", STOP_REASON_CANCELLED):
        return
    if all(info.get("done") for info in turn._native_tracker.values()):
        return
    flush = getattr(turn.slot._acp_client, "flush_native_child_text", None)
    if not callable(flush):
        return
    try:
        held = list(flush())
    except Exception:
        logger.debug("native child text flush failed", exc_info=True)
        return
    for event in held:
        if event.sub_session_id in turn._native_tracker and event.text:
            _native_card_text(turn, event.sub_session_id, event.text)
