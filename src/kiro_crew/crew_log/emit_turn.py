"""Turn facts: a turn's start and end, and what happens inside it.

A turn started, refused, completed or failed; the messages it received and sent; the request
configuration and the context composed for it; its model calls (steps); its tool calls and
approvals; the agent's plan; the model it selected; a compaction applied to it; the closers
:func:`close_open_tool_calls` writes for tool calls a turn left open; and a model call the
gateway made on the session's behalf outside any turn (``background/completed``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from kiro_crew.crew_log.emit_runtime import (
    _closing,
    _handle,
    _submit,
    _terminal_dropped,
    _tracker,
    _write,
)
from kiro_crew.crew_log.emit_state import (
    _MAX_OPEN_CREW_LOGS,
    _SRC_ACP,
    _SRC_GATEWAY,
    _last_config,
    _lock,
    _note_eager,
    enabled,
)
from kiro_crew.crew_log.emit_text import (
    _MAX_ID_TEXT,
    _MAX_PLAN_ITEMS,
    _MAX_SHORT_TEXT,
    _append_body_entry,
    _clip,
    _entry_line_fits,
    _payload_digest,
    _safe_text,
)

#: Characters per token, the repo's own estimate for a prompt it cannot tokenize
#: exactly (``dashboard/handlers/usage_sessions.py`` applies the same 4.0). Restated here
#: rather than imported: that module is a dashboard handler and this one is
#: imported BY the dashboard chat path, so importing it back would close an
#: import cycle. Any block count derived from it is an ESTIMATE and the spec says
#: so -- the only exact tokenizer available is the wrong one for the served model.
_EST_CHARS_PER_TOKEN = 4.0


#: The label a source with NO NAME AT ALL is reported under. ``split_blocks``
#: classifies by opening marker, and three blocks the design names -- steering, tool
#: specs, injected crew log context -- have none, so their characters land in its
#: ``unclassified`` bucket.
#:
#: That bucket keeps its own name and is NOT renamed here. It is a label the readers
#: already know: the Context panel carries a translated string for ``unclassified``
#: and none for ``other``, so folding the two together made a named remainder render
#: as an untranslated word in every shipped locale -- visible the moment that panel
#: started reading these sources instead of the token row's own ``split_blocks``
#: output. Only the empty label lands here, because an empty label names nothing and
#: a reader cannot be given a translation for it.
_OTHER_SOURCE = "other"

_UNCLASSIFIED_LABELS = frozenset({""})

#: Who caused a turn to run. Every value names a STRUCTURAL producer the
#: dispatch layer identifies; ``user`` means a person typed the message,
#: ``app`` means an installed app's backend sent it under its own token, and
#: ``gateway`` means the gateway synthesized it and no narrower producer fits
#: (a task-runner summary, an orchestrator stage). Anything unrecognised is
#: recorded as ``other`` rather than guessed.
#:
#: A reader never gates on this field, so a producer landing later widens the
#: set without breaking a reader built against the narrower one -- unlike an
#: entry TYPE, which aborts reconstruction when a reader does not know it.
ACTORS = frozenset({"user", "app", "crew", "cron", "autonudge", "subagent", "gateway", "other"})


def close_open_tool_calls(
    session_id: str,
    turn: int,
    *,
    status: str = "unknown",
    is_error: bool | None = None,
) -> int:
    """Close every tool call of *turn* that is still open. Returns how many.

    A tool that emits no terminal UPDATE sends no result frame at all, so
    ``tool_final`` never arrives and the completion path never runs: its
    ``tool/called`` would stay open for the life of the file, and a fold
    counting open calls would report a turn that never finished using a tool it
    had in fact finished with.
    The transcript already compensates for this on the runner's side -- text after
    a tool group means every tool in it is done -- and this is the same inference
    for the log.

    ``result_bytes`` is 0 rather than absent, and there is no ``result_hash``:
    the tool genuinely produced no bytes, which is a different claim from "the
    payload was not recorded". A closer written here carries no ``elapsed_ms``
    when the call frame is gone from memory, for the same reason.

    *status* is what separates the two callers, and the default is the SAFE one.
    At a tool-group boundary the inference above is grounded -- the model went on
    to say something, so the tools it was waiting on are done -- and that caller
    passes ``"completed"`` explicitly. At turn end nothing of the kind is known:
    the stream may have raised, the process may have died, and a call still open
    there has an outcome no site observed. Recording that as ``"completed"``
    states success in a file nothing rewrites, so the default is ``"unknown"`` --
    the same word ``repair_interrupted_turn`` writes for an unmatched
    ``tool/called``, which is the identical claim reached from the file instead of
    from memory.

    ``is_error`` stays absent on an unknown close rather than being set from the
    turn's own failure. A turn that raised says so in its own terminal; asserting
    the TOOL errored would be a second, unobserved claim.
    """
    if not session_id or not enabled():
        return 0
    # The sweep IS each call's closer, so the tracker marks each one settled: a
    # terminal frame arriving after the sweep closed the call must not write a second
    # ``tool/completed`` -- the same settle-once rule ``on_tool_completed`` applies.
    closed = 0
    for call in _tracker().sweep_open_calls(session_id, turn):
        data: dict[str, Any] = {
            "turn": int(turn),
            "call_id": call.call_id,
            "name": call.name,
            "server": call.server,
            "status": status,
            "result_bytes": 0,
        }
        if call.call_index:
            data["call_index"] = call.call_index
        if call.step:
            data["step"] = call.step
        data["elapsed_ms"] = call.elapsed_ms
        if is_error is not None:
            data["is_error"] = bool(is_error)
        _write(session_id, "tool/completed", data)
        closed += 1
    return closed


def on_turn_started(
    session_id: str,
    turn: int,
    actor: str = "user",
    *,
    depth: int = 0,
    message_seq: int = 0,
    attempt: int = 0,
) -> None:
    """Anchor a turn's thread. ``turn`` is the message-boundary ordinal.

    ``attempt`` is normally DERIVED, not passed: 0 means "work it out", which is
    what every call site uses, because a rerun reaches this through several paths
    and a field only the audited ones fill is a field a reader cannot trust. A
    positive value overrides the count, for a caller that genuinely knows better.
    """
    # Open the turn's live record BEFORE the write is queued: from here until its
    # terminal event the handle and the step counter are live state, and losing
    # either costs a reopen mid-turn or a repeated step ordinal.
    #
    # Behind the flag, like every other allocation here: with the emitter off this
    # module holds nothing, so a turn costs one env read. The RELEASE paths are
    # deliberately not guarded -- a flag turned off mid-turn must still free what
    # it allocated while on.
    if session_id and enabled():
        _tracker().begin(session_id, turn)
    data: dict[str, Any] = {
        "turn": int(turn),
        "actor": actor if actor in ACTORS else "other",
        "depth": int(depth),
    }
    # The seq of the message entry that caused this turn, when the caller knows
    # it. Omitted rather than zeroed when it does not: 0 is not a seq, and a
    # reader must be able to tell "no pointer" from "points at line 0".
    if message_seq > 0:
        data["message_seq"] = int(message_seq)
    if not session_id or not enabled():
        return

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            return
        # Derived HERE, on the writer thread, NOT on the caller's. Resume seeding
        # runs as an earlier job for this same session, and the writer executes a
        # session's jobs in submission order -- so deriving here is ordered behind
        # the seed by construction. Deriving on the caller's thread instead reads
        # the map while that seed is still queued, and the first rerun after a
        # resume then writes an attempt the file already holds. The alternative,
        # making the caller wait for a seeded event, puts a filesystem read in
        # front of the turn on the event loop.
        n = attempt if attempt > 0 else _tracker().next_attempt(session_id, turn)
        payload = dict(data)
        # Which try this is at the same turn ordinal. Omitted at 1, which is every
        # turn that was never rerun -- the common case should not pay a field for
        # the rare one.
        if n > 1:
            payload["attempt"] = int(n)
        entry = log.append("turn/started", payload, src=_SRC_GATEWAY)
        # A worker's turn moves its creator's ``workers`` page.
        _note_eager(entry, "turn/started", session_id, payload)

    _submit(_job, "appending turn/started", session_id)


def on_turn_refused(
    session_id: str,
    turn: int,
    reason: str,
    actor: str = "user",
    *,
    depth: int = 0,
) -> None:
    """Record a turn that was dispatched but never authorized to run.

    Its own fact rather than a ``turn/started`` with no completion. A started
    entry means the turn RAN, so writing one for a refusal would make a turn that
    never reached the model indistinguishable from one that died mid-flight, and
    the interrupted-turn repair would then close it as though it had. ``reason`` is
    the gateway's own word for which gate refused it.
    """
    _write(
        session_id,
        "turn/refused",
        {
            "turn": int(turn),
            "actor": actor if actor in ACTORS else "other",
            "reason": reason,
            "depth": int(depth),
        },
        src=_SRC_GATEWAY,
        after=_closing(session_id, turn),
        on_permanent_drop=_terminal_dropped(session_id),
    )


def on_turn_completed(
    session_id: str,
    turn: int,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    credits: float = 0.0,
    duration_ms: int = 0,
    stop_reason: str = "",
    model: str = "",
    provider: str = "",
    depth: int = 0,
    context_used: int = 0,
    context_window: int = 0,
) -> None:
    """Record a turn's terminal event and what it cost.

    ``context_used`` / ``context_window`` are the PROVIDER's own occupancy reading
    for this turn, and they are a different quantity from ``tokens`` beside them.
    ``tokens`` is what was BILLED: it is summed over every model call the turn made,
    so it answers what the turn cost. Occupancy answers how full the window was, and
    a reader wanting "how close to full did this session get" needs the second --
    dividing a billed total by a window size is not that number, and on a
    tool-using turn it is larger than the window it is divided by.

    The pair travels TOGETHER on one entry for the same reason: a used count from one
    turn over a window size from another describes no turn at all, and a model switch
    moves the window. Both are absent when the provider reports neither, so an
    unmeasured turn reads as unmeasured rather than as an empty window.

    ``tokens`` and ``credits`` follow the same rule, one field at a time: each is
    written only when the provider actually reported it. ``TurnUsage`` zero-fills
    every dimension a provider does not report, so at this seam a zero is "nothing
    was reported", not a measurement of nothing -- and a zero written as a
    measurement is what the ``usage`` fold would count as a reporting turn, putting
    a measured ``0 tokens`` beside a real bill. A present ``tokens`` keeps all FOUR
    dimensions, zeros included, because its schema requires each member and a zero
    INSIDE a reported block is a real zero; ``background/completed`` drops the zero
    members instead. The two writers agree on what an unreported count looks like
    -- absent -- and differ only in the shape of a reported one.
    """
    data = _turn_closer(
        turn,
        duration_ms=duration_ms,
        stop_reason=stop_reason,
        model=model,
        provider=provider,
        depth=depth,
    )
    # Positive and finite, or absent: the guard the two subagent closers use. A
    # provider that does not bill in credits reports 0.0 through ``TurnUsage``,
    # which is indistinguishable here from a free turn, so the zero is dropped and
    # absent keeps meaning unmetered.
    charge = float(credits)
    if charge > 0 and math.isfinite(charge):
        data["credits"] = charge
    tokens = {
        "input": int(input_tokens),
        "output": int(output_tokens),
        "cache_read": int(cache_read_tokens),
        "cache_write": int(cache_write_tokens),
    }
    if any(count > 0 for count in tokens.values()):
        data["tokens"] = tokens
    # Written only when the provider actually reported them. ``read_context_tokens``
    # answers (0, 0) for a provider without the accessors, and a stored zero would
    # be indistinguishable from a window of nothing.
    if int(context_used) > 0 or int(context_window) > 0:
        data["context"] = {"used": int(context_used), "window": int(context_window)}
    _write(
        session_id,
        "turn/completed",
        data,
        after=_closing(session_id, turn),
        on_permanent_drop=_terminal_dropped(session_id),
    )


def on_turn_failed(
    session_id: str,
    turn: int,
    *,
    error: str = "",
    duration_ms: int = 0,
    stop_reason: str = "failed",
    model: str = "",
    provider: str = "",
    depth: int = 0,
) -> None:
    """Close a turn that ended WITHOUT its terminal event, observed in process.

    A stream that raises, or a recovery path that returns before the terminal
    event, ends the turn while the writer is still alive and watching. Leaving the
    ``turn/started`` open would make that indistinguishable from a turn whose
    writer was killed mid-flight -- and nothing in this process would ever close
    it, since the interrupted-turn repair is opt-in and only a RESUME asks for it.
    A later resume would then close it as an interruption that never happened,
    stamped at the last real entry's time. So the observation is recorded where it
    is made.

    ``tokens`` and ``credits`` are ABSENT rather than zeroed, and that absence is
    the record: no usage event arrived, so nothing was measured, and a turn that
    streamed real text does not get a durable line claiming it cost nothing. The
    measured closer omits them too when its provider reported nothing, so absence
    does not tell this closer from a provider-reported one; ``stop_reason`` does.
    ``duration_ms`` IS measured -- the turn's own elapsed time -- and ``error``
    names the exception CLASS when one was caught, never its message, which can
    carry a path or a credential.
    """
    data = _turn_closer(
        turn,
        duration_ms=duration_ms,
        stop_reason=stop_reason or "failed",
        model=model,
        provider=provider,
        depth=depth,
    )
    if error:
        data["error"] = error
    _write(
        session_id,
        "turn/completed",
        data,
        src=_SRC_GATEWAY,
        after=_closing(session_id, turn),
        on_permanent_drop=_terminal_dropped(session_id),
    )


def _turn_closer(
    turn: int,
    *,
    duration_ms: int,
    stop_reason: str,
    model: str,
    provider: str,
    depth: int,
) -> "dict[str, Any]":
    """The fields every ``turn/completed`` carries, whichever path closes the turn.

    Shared so the measured closer and the failed one cannot drift into two
    different shapes for one type -- the difference between them is which fields
    they ADD, never which of these they leave out.
    """
    return {
        "turn": int(turn),
        "depth": int(depth),
        "stop_reason": stop_reason,
        "duration_ms": int(duration_ms),
        "model": model,
        "provider": provider,
    }


def on_message_received(
    session_id: str,
    turn: int,
    *,
    role: str = "user",
    text: str = "",
    source: str = "",
    attachments: "tuple[str, ...] | list[str]" = (),
) -> None:
    """Record the body of a message the gateway accepted into this session.

    Written BEFORE the dispatch gates, so a turn that is refused still shows what
    was said. The alternative -- emitting beside ``turn/started`` -- would lose the
    body of exactly the turns a reader most wants to explain.

    Not emitted where the message is actually appended to the slot
    (``_ChatSlot.enqueue_or_run_prompt``): the crew log is keyed by the ACP session
    id and that site runs before the session is claimed, so there is no id to key
    by and no crew log to write to yet.

    ``source`` is the surface the message arrived on, a fact the dispatch layer
    supplies. ``attachments`` are identifiers, not ``Ref``s -- a ``Ref`` cites
    lines of another crew log and an attachment is not a crew log unit, the same
    reason a tool call id lives in ``data``.

    Gated on the flag before redaction runs: redaction walks the whole body with
    a set of patterns, once per message on the event loop, so a disabled emitter
    must not pay for it.

    The body goes through :func:`_append_body_entry` like every other body, which
    is what gives a pasted message too large for one line the same split the
    assistant side gets. Writing ``text`` directly here would refuse the append
    and lose the message whole.
    """
    if not session_id or not enabled():
        return
    body = _safe_text(text)
    extra: dict[str, Any] = {"role": role, "source": source}
    names = [str(item) for item in attachments if item]
    if names:
        extra["attachments"] = names

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            return
        entry = _append_body_entry(
            log,
            "message/received",
            turn,
            text=body,
            extra=extra,
            src=_SRC_GATEWAY,
        )
        if entry is not None:
            _note_eager(entry, "message/received", session_id, entry.data)

    _submit(_job, "appending message/received", session_id, len(body))


def on_message_sent(
    session_id: str,
    turn: int,
    *,
    step: int = 0,
    text: str = "",
    interrupted: bool = False,
) -> None:
    """Record a finished assistant message -- one model call's worth of text.

    The body goes through :func:`_append_body_entry`, the single place a body
    becomes fields: it splits a text too big for one line into ``message/chunk``
    entries the ``message/sent`` then cites in ``chunks``, so the whole text is
    recoverable in order and the 64 KiB ceiling still holds. Those overflow chunks
    carry a body that would otherwise be lost outright, and they are safe to write
    for a reason the removed streaming emitter could not satisfy: the body is
    redacted ONCE, whole, before it is sliced, so a credential cannot straddle two
    slices unmatched.

    All of it happens in ONE queued job. The chunks have to be on disk before the
    entry citing them can name their seqs, and splitting the work across jobs would
    let another entry land between a chunk and its citation.

    No ``usage``. The design's field is real but the runtime measures usage per
    TURN, not per assistant message, and the turn's numbers already ride on
    ``turn/completed``; dividing them across a turn's messages would be a guess
    presented as a measurement.
    """
    if not session_id or not enabled():
        return
    body = _safe_text(text)
    if not body:
        return
    extra: "dict[str, Any] | None" = {"interrupted": True} if interrupted else None
    # A caller that does not name the model call gets the one this turn is on.
    # Derived from the live record rather than asked for, so a caller holding only
    # a slot -- the segment flush does -- has nothing to thread through and nothing
    # to get stale.
    ordinal = int(step) if step else _tracker().current_step(session_id, turn)

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            return
        entry = _append_body_entry(
            log,
            "message/sent",
            turn,
            step=ordinal,
            text=body,
            extra=extra,
            src=_SRC_ACP,
        )
        if entry is not None:
            _note_eager(entry, "message/sent", session_id, entry.data)

    _submit(_job, "appending message/sent", session_id, len(body))


def on_request_configured(
    session_id: str,
    turn: int,
    *,
    model: str = "",
    provider: str = "",
    system: str = "",
    context_window: int = 0,
) -> None:
    """Record the request configuration, but only when it CHANGED.

    Rewriting an identical configuration every turn would bury the turns where it
    actually moved, and those are the only ones a reader wants from this entry --
    a model swap, a provider fallback, a window that grew. So the last one written
    is remembered per session and an unchanged configuration is silent.

    ``system`` is a DIGEST of the system prompt, never its text: it is long, it is
    identical across most turns, and what a reader needs is whether it changed.

    No ``tools`` list. The design asks for one and the gateway cannot supply it:
    tool specs are served to the model by the backend with tool search on, and the
    only inventory this process holds is the per-stub surface the MCP gateway
    projected, which is keyed by stub rather than by session and carries names
    without spec sizes. An empty list every turn would read as "no tools", which
    is false, so the field is absent instead.
    """
    if not session_id or not enabled():
        return
    system_hash, system_bytes = _payload_digest(system) if system else ("", 0)
    fingerprint = (model, provider, system_hash, int(context_window))
    with _lock:
        if _last_config.get(session_id) == fingerprint:
            return
    data: dict[str, Any] = {
        "turn": int(turn),
        "model": model,
        "provider": provider,
        "context_window": int(context_window),
    }
    if system_hash:
        data["system"] = system_hash
        data["system_bytes"] = system_bytes

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            return
        log.append("request/configured", data, src=_SRC_GATEWAY)
        # Remembered only AFTER the line is on disk. Committing the fingerprint
        # before the append would let one transient failure suppress every later
        # identical configuration, leaving the session permanently without the
        # record -- a silent hole rather than a retry. The other direction costs a
        # duplicate entry when two turns queue before the first lands, and a
        # duplicate in an append-only log is a far cheaper wrong than a gap.
        with _lock:
            _last_config[session_id] = fingerprint
            _last_config.move_to_end(session_id)
            _tracker().trim(_last_config, _MAX_OPEN_CREW_LOGS, lambda k: k, "request configs")

    _submit(_job, "appending request/configured", session_id)


def on_context_composed(
    session_id: str,
    turn: int,
    *,
    step: int = 0,
    blocks: "dict[str, int] | None" = None,
    total_chars: int = 0,
    phase: str = "",
    omitted: "dict[str, dict[str, Any]] | None" = None,
) -> None:
    """Record what the gateway put in front of the model, block by block.

    ``blocks`` is ``context_blocks.split_blocks()``'s own output: a label to
    CHARACTER count map whose values sum to the assembled prompt. Characters are
    what the repo measures exactly, so they are recorded as measured and
    ``tokens`` is derived at :data:`_EST_CHARS_PER_TOKEN` -- an ESTIMATE, and the
    spec says so. The one tokenizer available is the wrong one for the served
    model, and a fabricated exact count would be worse than an admitted estimate.

    Labels pass through as ``split_blocks`` named them, including its
    ``unclassified`` remainder -- three blocks the design names (steering, tool specs
    and injected crew log context) have no opening marker, so their characters are
    genuinely in that bucket, and reporting them as three zeroed sources would claim a
    measurement nobody took. Only a source whose label is EMPTY is renamed, to
    :data:`_OTHER_SOURCE`; see there for why the named remainder keeps its name.

    ``phase`` says which POPULATION this composition belongs to
    (:data:`~kiro_crew.context_blocks.PHASE_SESSION_START` or
    :data:`~kiro_crew.context_blocks.PHASE_PER_TURN`). A session-start injection is
    many times the size of a per-turn one, so a reader that cannot separate them
    either pools two populations into one meaningless distribution or lets the
    single largest composition set the scale for every other. Only the composer
    knows which it built, so the field is recorded here and DERIVED nowhere: an
    unstated phase is left absent, because the nearest available guess -- the first
    composition in a unit -- is wrong for the rebuild a replay triggers mid-session.

    ``omitted`` is what the composer's budgets CUT from this composition, keyed by
    block kind, as :func:`kiro_crew.context_omissions.collect` gathered it from the
    decisions that wrote the prompt's omission notices: counts, budgets and a reason,
    never omitted content. It is written only when something was cut, so a reader
    treats an absent field as "nothing omitted".
    """
    if not session_id or not enabled() or not blocks:
        return
    tallied: dict[str, int] = {}
    for label, chars in blocks.items():
        try:
            count = int(chars)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        key = _OTHER_SOURCE if label in _UNCLASSIFIED_LABELS else str(label)
        tallied[key] = tallied.get(key, 0) + count
    if not tallied:
        return
    sources = [
        {"kind": kind, "chars": chars, "tokens": int(round(chars / _EST_CHARS_PER_TOKEN))}
        for kind, chars in sorted(tallied.items(), key=lambda item: (-item[1], item[0]))
    ]
    chars_total = int(total_chars) or sum(tallied.values())
    data: dict[str, Any] = {
        "turn": int(turn),
        "sources": sources,
        "chars": chars_total,
        "tokens": int(round(chars_total / _EST_CHARS_PER_TOKEN)),
        "tokens_estimated": True,
    }
    if step:
        data["step"] = int(step)
    if phase:
        data["phase"] = str(phase)
    if omitted:
        # ``context_omissions.record`` is the only writer of this mapping and admits
        # only the vocabulary the entry declares, so it passes through as recorded.
        data["omitted"] = {str(kind): dict(entry) for kind, entry in omitted.items()}
    _write(session_id, "context/composed", data, src=_SRC_GATEWAY)


def on_context_measured(
    session_id: str,
    turn: int,
    *,
    profile: "Mapping[str, Any] | None" = None,
) -> None:
    """Record what the agent engine measured of this turn, beside what it composed.

    ``profile`` is :attr:`~kiro_crew.acp.types.AcpPromptStats.kas_profile`, already
    reduced to numbers by :mod:`kiro_crew.acp.kas_profile`: ``breakdown`` is the
    engine's split of the turn's LAST model request into categories, in characters,
    and ``ttft`` its time to first displayed output. Either may be absent, and with
    neither there is nothing to record, so nothing is written: a turn without a
    measurement reads as unmeasured, never as a measurement of zero.

    Written by the turn's closer just before ``turn/completed``, so the ``usage``
    fold finds the turn's composition row still at the tail of its window.
    """
    if not session_id or not enabled() or not profile:
        return
    data: dict[str, Any] = {"turn": int(turn)}
    for key in ("breakdown", "ttft"):
        value = profile.get(key)
        if isinstance(value, Mapping) and value:
            data[key] = dict(value)
    if len(data) == 1:
        return
    turn_id = profile.get("turn_id")
    if isinstance(turn_id, str) and turn_id:
        data["turn_id"] = turn_id
    _write(session_id, "context/measured", data, src=_SRC_GATEWAY)


def on_step_started(session_id: str, turn: int) -> int:
    """Open a model call inside *turn* and return its ordinal.

    A step is ONE model call. A turn is several: the model speaks, calls tools,
    and is called again with their results. The stream carries no per-call event --
    its terminal event is the turn's -- so the boundary is synthesized at the one
    transition that is observable, a tool group followed by fresh text, and the
    spec records that this is a derived boundary rather than a reported one.

    Returns the ordinal so the caller can pass it to the entries produced inside
    the call and to :func:`on_step_completed`, without reading it back.
    """
    if not session_id or not enabled():
        return 0
    step = _tracker().next_step(session_id, turn)
    _write(
        session_id,
        "step/started",
        {"turn": int(turn), "step": step},
        src=_SRC_GATEWAY,
    )
    return step


def on_step_completed(session_id: str, turn: int, step: int, *, ms: int = 0) -> None:
    """Close a model call and record how long it took."""
    if not step:
        return
    _write(
        session_id,
        "step/completed",
        {"turn": int(turn), "step": int(step), "ms": max(0, int(ms))},
        src=_SRC_GATEWAY,
    )


def on_message_queued(
    session_id: str,
    *,
    source: str = "",
    size_bytes: int = 0,
    queued_seq: str = "",
) -> None:
    """Record a message that arrived while a turn was already running.

    No ``turn``, and the absence is the record: a queued message belongs to no
    turn yet. It names the turn it eventually runs as when that turn starts, and
    stamping the RUNNING turn's ordinal here would attribute one person's message
    to another's turn.

    The body is not recorded. It is recorded by ``message/received`` when the
    queue drains and the message is actually accepted, so writing it twice would
    put the same body in the log under two facts. Its SIZE is recorded, which is
    what a reader asks of a queue.
    """
    _write(
        session_id,
        "message/queued",
        {"source": source, "bytes": max(0, int(size_bytes)), "queued_seq": str(queued_seq or "")},
        src=_SRC_GATEWAY,
    )


def on_tool_called(
    session_id: str,
    turn: int,
    *,
    name: str,
    server: str = "",
    kind: str = "",
    call_id: str = "",
    args: str = "",
) -> None:
    """Record a tool call by its id. Arguments are digested, never recorded.

    Two ordinals, and they answer different questions. ``call_index`` is this
    call's position among the turn's calls, in the order the runner issued them.
    ``step`` is the model call that issued it -- one model call can issue several
    tools at once, so it cannot order them, and the pair together says both which
    request caused the call and where it sat in the sequence.

    The flag is checked HERE, not just in :func:`_write`. Digesting a payload is
    proportional to its size and this runs once per tool frame on the event loop,
    so a disabled emitter that still digested would charge every user for a
    feature they do not have.
    """
    if not session_id or not enabled():
        return
    tracker = _tracker()
    call_index = tracker.next_call_index(session_id, turn)
    step = tracker.current_step(session_id, turn)
    if call_id:
        tracker.call_opened(
            session_id,
            call_id,
            name=name,
            server=server,
            call_index=call_index,
            step=step,
            turn=turn,
        )
    data: dict[str, Any] = {
        "turn": int(turn),
        "call_id": call_id,
        "name": name,
        "server": server,
        "kind": kind,
    }
    if call_index:
        data["call_index"] = call_index
    if step:
        data["step"] = step
    args_hash, args_bytes = _payload_digest(args)
    if args_hash:
        data["args_hash"] = args_hash
        data["args_bytes"] = args_bytes
    _write(session_id, "tool/called", data)


def on_tool_completed(
    session_id: str,
    turn: int,
    *,
    name: str = "",
    server: str = "",
    status: str = "",
    call_id: str = "",
    is_error: bool | None = None,
    result: str = "",
    result_digest: str = "",
    result_bytes: int = -1,
) -> None:
    """Record a tool call's terminal frame. Results are digested, never recorded.

    The terminal frame does not repeat the tool's identity -- only the call
    frame carries the trusted name and server -- so both are remembered per
    call id and filled in here rather than being recorded empty. The call's
    ``call_index`` and ``step`` ride along the same way, so a completion sits at
    the same position in the turn, and inside the same model call, as the call it
    closes.

    ``is_error`` is tri-state: ``None`` means the caller did not say, which is
    not the same claim as ``False``, so it is left off the entry rather than
    recorded as a success nobody asserted.

    Gated on the flag here for the same reason as the call: a tool RESULT is the
    largest payload this module ever digests.
    """
    if not session_id or not enabled():
        return
    elapsed_ms = -1
    call_index = 0
    step = 0
    if call_id:
        # A tool call settles exactly ONCE. Both update parsers can produce a
        # status-only terminal frame for the same id, so a second frame arriving
        # after the first closed the call must add NOTHING -- otherwise one call gets
        # two ``tool/completed`` entries. The tracker answers None for a call it has
        # already settled; a call whose ``tool/called`` it never saw still settles and
        # still gets its closer, with empty name/server and no elapsed.
        settled = _tracker().call_settled(session_id, call_id, turn)
        if settled is None:
            return
        elapsed_ms = settled.elapsed_ms
        name = name or settled.name
        server = server or settled.server
        call_index = settled.call_index
        step = settled.step
    data: dict[str, Any] = {
        "turn": int(turn),
        "call_id": call_id,
        "name": name,
        "server": server,
        "status": status,
    }
    if call_index:
        data["call_index"] = call_index
    if step:
        data["step"] = step
    if elapsed_ms >= 0:
        data["elapsed_ms"] = elapsed_ms
    if is_error is not None:
        data["is_error"] = bool(is_error)
    if result_bytes < 0:
        result_digest, result_bytes = _payload_digest(result)
    if result_digest:
        data["result_hash"] = result_digest
    if result_bytes >= 0:
        data["result_bytes"] = result_bytes
    _write(session_id, "tool/completed", data)


def on_approval_requested(
    session_id: str,
    turn: int,
    *,
    approval_id: str,
    tool: str = "",
    reason: str = "",
) -> None:
    """Record that a tool call is waiting on a human.

    ``reason`` is what the human is being shown -- the card's title, or the
    command for a shell request. It arrives already display-redacted by the ACP
    transport and is redacted again here, because this module redacts at its own
    boundary rather than trusting a call site, and clipped so a long command
    cannot push the entry past the line ceiling and lose the whole fact.

    ``tool`` and ``reason`` are each absent rather than empty when the site had
    nothing to name. A permission frame can arrive without a resolvable tool name,
    and writing ``""`` there would record "the tool is the empty string" -- a value
    a reader cannot tell from a real one, in a log whose entire worth is that it
    only says what was observed.

    Guarded on the flag here for the same reason :func:`on_plan_updated` is: the
    redaction below runs before :func:`_write` gets its own chance to no-op, and the
    runner calls this unconditionally.
    """
    if not session_id or not enabled():
        return
    data: dict[str, Any] = {"turn": int(turn), "approval_id": approval_id}
    if tool:
        data["tool"] = tool
    shown = _clip(_safe_text(reason), _MAX_SHORT_TEXT)
    if shown:
        data["reason"] = shown
    _write(session_id, "approval/requested", data, src=_SRC_GATEWAY)


def on_approval_decided(
    session_id: str,
    turn: int,
    *,
    approval_id: str,
    decision: str,
    by: str = "",
    cause: str = "",
) -> None:
    """Record how an approval resolved, including a timeout.

    This runs on the task that handled the click, not the task running the
    turn, which is why it must never raise.

    ``by`` names WHO decided, and only the host itself can be named with
    certainty: an auto-decline the gateway made is attributable, so it says
    ``host``. A decision that came back through the approval future was made by a
    person at one of several surfaces -- dashboard click, Slack button -- and the
    site cannot see which, so it omits the field rather than asserting ``user``
    for something it did not observe.

    ``cause`` is WHY, and only a host decline has one: the gateway's own reason
    code for declining without a human (the window expired, the turn had no
    budget left, the prompt could not be delivered). It rides in its own field
    instead of replacing ``decision``, so a reader still learns what was decided
    and does not have to know the reason vocabulary to find out.
    """
    data: dict[str, Any] = {
        "turn": int(turn),
        "approval_id": approval_id,
        "decision": decision,
    }
    if by:
        data["by"] = by
    if cause:
        data["cause"] = cause
    _write(session_id, "approval/decided", data, src=_SRC_GATEWAY)


def on_plan_updated(session_id: str, turn: int, *, items: Any) -> None:
    """Record the agent's own task list as the agent just restated it.

    A TODO update is a WHOLE list, not a delta: the agent re-sends every task on
    every change, so the entry is the list as of this update and a reader diffs
    consecutive entries itself. ``items`` is the stream's own ``tasks`` array of
    ``{id, text, completed}``. ``None`` means the event said nothing about the
    plan and no entry is written; an empty LIST means the agent cleared its plan,
    which is a change and is recorded as one.

    ``state`` is two-valued -- ``done`` / ``open`` -- because that is all the
    stream carries. The backend's todo model is a plain ``completed`` boolean with
    no in-progress state, as the slot's own snapshot code documents, so a
    three-state vocabulary would be invented here and is not written.

    The list is bounded twice, by COUNT and by BYTES, and both bounds keep the real
    count in ``total`` so a clipped record still says how much it is not showing.
    Count alone is not enough: ``_clip`` bounds each ``text`` in characters while the
    store serializes with ``ensure_ascii``, which spends six bytes on a BMP character
    and twelve on a surrogate pair -- so a hundred separately-legal rows of emoji
    serialize past the entry ceiling, where the append is REFUSED and the whole
    update disappears. Measured through the store's own serializer, because that is
    what the writer will measure.

    Guarded on the flag HERE rather than relying on :func:`_write`'s own guard,
    because this function does real work before it reaches one: a redaction per task
    and a serialize probe per admitted row, on the chat loop, for a feature that can
    be switched off. The subagent and background emitters are guarded at their callers
    instead; this and :func:`on_approval_requested` are the two the runner calls
    unconditionally, so they carry their own.
    """
    if not session_id or not enabled():
        return
    rows: list[dict[str, Any]] = []
    total = 0
    #: Set by the first row the line cannot hold. Every later row is then counted
    #: and not admitted, because ``items`` is read as the FRONT of the plan: a
    #: shorter row admitted past a dropped one would make it a subsequence, and a
    #: reader diffing consecutive entries would see tasks reorder and vanish.
    full = False
    if items is None:
        # Not the same as a plan of zero tasks. The event carried no task list at
        # all, so nothing was observed about the plan, and an entry claiming it is
        # now empty would be an invention. An event that DOES carry an empty list
        # is a cleared plan and is recorded as one.
        return
    for task in items:
        if not isinstance(task, dict):
            continue
        total += 1
        if full or len(rows) >= _MAX_PLAN_ITEMS:
            continue
        row = {
            "id": _clip(_safe_text(str(task.get("id") or len(rows) + 1)), _MAX_ID_TEXT),
            "text": _clip(_safe_text(task.get("text")), _MAX_SHORT_TEXT),
            "state": "done" if task.get("completed") else "open",
        }
        # Measured against the entry it is about to join, and the widest form of
        # that entry: `total` is included so admitting this row cannot be what
        # pushes the finished line over once the count field appears.
        probe = {"turn": int(turn), "items": rows + [row], "total": total}
        if rows and not _entry_line_fits("plan/updated", probe, src=_SRC_ACP):
            full = True
            continue
        rows.append(row)
    data: dict[str, Any] = {"turn": int(turn), "items": rows}
    if total > len(rows):
        data["total"] = total
    # A sampled stream: the agent overwrites its plan freely and nothing later in
    # the file depends on any single update having been read.
    _write(session_id, "plan/updated", data, src=_SRC_ACP, ignorable=True)


def on_background_completed(
    session_id: str,
    *,
    kind: str,
    model: str = "",
    provider: str = "",
    credits: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    duration_ms: int = 0,
) -> None:
    """Record a model call the gateway made ON this session's behalf.

    Titling, summarizing and memory consolidation spend the user's budget without
    the user asking, and until now that spend appeared in the usage store with no
    trace in the session it was charged to. This is that trace.

    No ``turn``. The call is not part of one -- it runs after a turn ends, on a
    separate background session -- and naming the turn that happened to be last
    would attribute the cost to work that did not cause it.

    ``tokens`` and ``credits`` are written only when a dimension was actually
    billed, following ``turn/completed``: a provider fills the dimensions it bills
    in and leaves the rest at 0, so a zero is "this provider does not bill here",
    not a measurement. ``duration_ms`` is the wall clock the background helper
    measured around the call itself, and it is likewise omitted at 0.
    """
    data: dict[str, Any] = {"kind": kind}
    if model:
        data["model"] = model
    if provider:
        data["provider"] = provider
    if credits:
        data["credits"] = float(credits)
    tokens = {
        "input": int(input_tokens),
        "output": int(output_tokens),
        "cache_read": int(cache_read_tokens),
        "cache_write": int(cache_write_tokens),
    }
    if any(tokens.values()):
        data["tokens"] = {name: count for name, count in tokens.items() if count}
    if duration_ms > 0:
        data["ms"] = int(duration_ms)
    _write(session_id, "background/completed", data, src=_SRC_GATEWAY)


def on_model_selected(session_id: str, model: str, source: str = "", *, turn: int = 0) -> None:
    """Record the model a session will serve and why it was chosen.

    ``turn`` is the turn the pick was made for. The fallback swap happens inside
    a running turn, so the site knows the ordinal and passes it; a pick made
    outside any turn records none rather than a placeholder.
    """
    data: dict[str, Any] = {"model": model, "source": source}
    if turn:
        data["turn"] = int(turn)
    _write(session_id, "model/selected", data, src=_SRC_GATEWAY)


def on_compaction_applied(
    session_id: str,
    *,
    pct_before: float,
    pct_after: float,
) -> None:
    """Record a compaction as context-usage percentages.

    No ``turn``, and the absence is the honest record: compaction is decided by
    the session's context meter between turns, and the settle path that confirms
    its effect can run turns later than the compaction it measures. Stamping one
    of those turns on the entry would name a turn that did not cause it.

    The compaction boundary measures ``provider.context_usage_pct()`` and never
    learns a raw token count, so this records what the site knows.
    """
    _write(
        session_id,
        "compaction/applied",
        {
            "pct_before": round(float(pct_before), 4),
            "pct_after": round(float(pct_after), 4),
            "freed_pct": round(float(pct_before) - float(pct_after), 4),
        },
        src=_SRC_GATEWAY,
    )
