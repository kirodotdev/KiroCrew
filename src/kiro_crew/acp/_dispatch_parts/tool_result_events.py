"""A ``tool_call_update``'s output, turned into ``EVENT_TOOL_RESULT``.

The content-block and ``rawOutput`` text (the MCP result envelope included), the
unrenderable-content diagnostic ``AcpClient`` shares, the output measure the crew log
records, and the ``todo_list`` task snapshot.

:mod:`kiro_crew.acp._dispatch` stays the import path and the patch surface; see
:mod:`kiro_crew.acp._dispatch_parts`.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from kiro_crew import mcp_apps_render, session_directive
from kiro_crew.acp._dispatch_parts.surfaced_text import (
    _dumps_degraded,
    _loggable_request_id,
    _redact,
    redact_text,
)
from kiro_crew.acp._dispatch_parts.tool_call_identity import _kiro_tool_name
from kiro_crew.acp._dispatch_parts.tool_call_provenance import scoped_tool_cache_key
from kiro_crew.acp.types import (
    EVENT_TOOL_RESULT,
    KIRO_TOOL_TODO_LIST,
    TERMINAL_TOOL_STATUSES,
    TODO_ID_MAX,
    TODO_TASKS_MAX,
    TODO_TEXT_MAX,
    AcpEvent,
)
from kiro_crew.metrics.tool_calls import record_tool_call_finished
from kiro_crew.security.credential_sources import tool_output_fingerprints

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.acp._dispatch")


def _mcp_content_text(payload: dict[str, Any]) -> str | None:
    """Return the text of an MCP tool-result envelope, or None if not one.

    An MCP ``tools/call`` result is ``{"content": [{"type": "text", "text": ...}]}``
    and kiro-cli forwards that dict verbatim as a ``rawOutput`` ``Json`` item.
    Serialising it with ``json.dumps`` escapes the payload — quotes become ``\\"``
    and non-ASCII becomes ``\\uXXXX`` — so any structured marker carried INSIDE the
    text is destroyed while still LOOKING intact to a human reading the transcript.
    That breaks session-directive tools: the directive's sentinel survives
    visually but stops matching, so the effect is dropped with no error.
    Extracting the inner text keeps the payload byte-exact.

    Returns None for anything that is not a pure text envelope, so genuinely
    structured payloads still fall back to ``json.dumps``.
    """
    blocks = payload.get("content")
    if not isinstance(blocks, list) or not blocks:
        return None
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            return None
        text = block.get("text")
        if not isinstance(text, str):
            return None
        parts.append(text)
    if not parts:
        return None
    return "\n".join(parts)


# ACP tool-call ``content`` entry types this parser understands: ``content``
# wraps a ContentBlock, ``diff`` becomes a unified diff in the tool_call
# builder (``_build_tool_call_event``), ``terminal`` is a live-terminal handle
# that carries no text, and ``text`` is the tolerated BARE ContentBlock form
# accepted by :func:`tool_call_content_text`.
_KNOWN_TOOL_CONTENT_TYPES = frozenset({"content", "diff", "terminal", "text"})

# ContentBlock ``type`` values a WRAPPED ``{"type": "content"}`` entry can carry.
# Only ``text`` renders here; the other four legitimately carry no text for this
# parser, so they must stay SILENT or the diagnostic fires on healthy frames.
#
# Needed because a known OUTER type is not evidence the entry is renderable: the
# wrapper is what this parser understands, and the block inside it is a second
# place the shape can be wrong. `resource_link` is spelled as the protocol spells
# it -- snake, matching the wire values the ACP tests use.
#
# An allowlist, so a block type added to the protocol later reports as a shape
# until this set learns it. That is the safe direction: such an entry renders no
# text either way, so the warning describes a real empty render rather than
# suppressing one.
_KNOWN_CONTENT_BLOCK_TYPES = frozenset({"text", "image", "audio", "resource", "resource_link"})

# Distinguishes "no ``content`` key" from ``"content": null``. A plain
# ``.get("content")`` collapses them, and they are not the same report: the first
# is a wrapper carrying no claim, the second is a backend explicitly asserting a
# null block.
_INNER_ABSENT = object()

# Bounds for the unrenderable-shape diagnostic below. Both exist because the
# frame is unbounded backend input and the warning it feeds is RETAINED in the
# log ring `/api/logs` serves, so an oversized one is held in memory rather than
# scrolling away.
_MAX_SHAPE_DIAGNOSTIC = 4000
_MAX_SHAPE_KEYS = 20


def tool_call_content_text(entry: Any) -> str | None:
    """Text of one ACP tool-call ``content`` entry, or None if it carries none.

    ACP's canonical entry WRAPS the ContentBlock:
    ``{"type": "content", "content": {"type": "text", "text": ...}}``. Real
    backends also send the ContentBlock BARE -- ``{"type": "text", "text": ...}``
    -- and that form is unambiguous, so it is read as if it had been wrapped.
    Rejecting it drops every entry of the frame, so the dashboard has nothing
    to render and falls through to "No input or output captured for this tool
    call." while the backend believes it reported the result.
    """
    if not isinstance(entry, dict):
        return None
    inner = entry.get("content")
    if not isinstance(inner, dict) and entry.get("type") == "text":
        # Bare ContentBlock: the entry IS the block it should have wrapped.
        inner = entry
    if not isinstance(inner, dict) or inner.get("type") != "text":
        return None
    text = inner.get("text")
    return str(text) if text else None


def _rendered_shape_type(value: Any) -> str:
    """A frame-supplied ``type`` made safe to put in the shape diagnostic.

    A string renders by ``repr`` -- the type NAME is the diagnostic. Anything else
    renders by CLASS, because ``repr()`` of a dict or list prints its nested
    values, so ``{"type": {"token": "..."}}`` would emit exactly what the
    diagnostic promises to withhold.
    """
    return repr(value) if isinstance(value, str) else f"<{type(value).__name__}>"


def _rendered_key_names(entry: dict[Any, Any]) -> list[str]:
    """Key NAMES of one entry, capped structurally.

    The cap drops WHOLE names and appends a count of the rest, rather than cutting
    through one -- a cut could halve a credential-shaped key name ahead of
    redaction.
    """
    names = sorted(str(k) for k in entry)
    shown = names[:_MAX_SHAPE_KEYS]
    if len(names) > _MAX_SHAPE_KEYS:
        shown.append(f"+{len(names) - _MAX_SHAPE_KEYS} more")
    return shown


def unrenderable_content_shapes(blocks: Any) -> str:
    """Shapes of ``content`` entries this parser cannot render text from.

    Empty string for a frame whose every entry is a known ACP entry type --
    including the ones that legitimately carry no text, such as an image
    ContentBlock -- so a working backend never triggers the caller's warning.

    Checked at BOTH levels. A known outer ``type`` is not evidence the entry is
    renderable: ``{"type": "content"}`` is a wrapper, and a malformed block inside
    it produces the same silent blank as an unknown entry. The line drawn is what
    the backend CLAIMED -- a wrapper whose ``content`` key is absent asserts no
    block and stays silent, while a ``content`` that is present but not a readable
    block is reported: not a dict, or carrying a ``type`` outside
    :data:`_KNOWN_CONTENT_BLOCK_TYPES`.

    Renders TYPE values and KEY names. It does NOT render any other value, at
    either level, and enforcing that takes one non-obvious step: ``type`` is
    frame-supplied and need not be a string, and ``repr()`` of a dict or list
    prints its nested VALUES -- so ``{"type": {"token": "..."}}`` would emit the
    very thing this function withholds. A non-string ``type`` is therefore
    rendered by CLASS (``type=<dict>``), never by repr. See
    :func:`_rendered_shape_type`, which both levels share.

    The caller writes the result into a warning that lands in the log ring
    ``/api/logs`` serves, which persists beyond the transcript's own redaction, so
    the redaction happens here rather than at each caller -- that keeps the two
    drop sites from diverging.

    Shapes are collected RAW and :func:`redact_text` runs ONCE over their JOIN,
    never per shape, with the single text bound applied AFTER that. Same ordering
    as ``_build_tool_result_event``'s output and for the same reason: redacting
    fragment-by-fragment lets a credential that straddles two entries survive as
    two halves that no single pattern matches, and cutting before redacting can
    split one the same way.

    Bounded THREE ways, because ``blocks`` is unbounded backend input and a final
    ``[:4000]`` alone still builds the whole list and join first -- a near-limit
    frame of unrecognised entries could allocate its way to an OOM before the cut
    ever ran:

    * entries stop being collected once the running length passes the budget;
    * each entry renders at most ``_MAX_SHAPE_KEYS`` key names, plus a count of
      the rest -- a STRUCTURAL truncation that drops whole names rather than
      cutting through one, so it cannot halve a credential ahead of redaction;
    * the redacted join is bounded once at the end.
    """
    if not isinstance(blocks, list):
        return ""
    shapes: list[str] = []
    budget = _MAX_SHAPE_DIAGNOSTIC
    for entry in blocks:
        if budget <= 0:
            break
        if not isinstance(entry, dict):
            shape = type(entry).__name__
        else:
            entry_type = entry.get("type")
            if isinstance(entry_type, str) and entry_type in _KNOWN_TOOL_CONTENT_TYPES:
                if entry_type != "content":
                    continue
                # A known WRAPPER is not a renderable entry. The block inside it is
                # a second place the shape can be wrong, and the failure looks
                # identical from the dashboard: `{"type": "content", "content":
                # {"kind": "text", ...}}` -- `kind` where the reader wants `type` --
                # renders nothing and, checked only at the outer level, said nothing
                # either. That is the same undiagnosable blank this function exists
                # to eliminate, one level down.
                inner = entry.get("content", _INNER_ABSENT)
                if inner is _INNER_ABSENT:
                    # No block ASSERTED at all. Silent, deliberately: this is the
                    # bare wrapper `_KNOWN_TOOL_CONTENT_TYPES` documents as
                    # understood, and warning here would fire on a backend that
                    # sends a wrapper before it has a block to put in it. The line
                    # this draws is "the backend claimed a block and got it wrong"
                    # (reported) versus "the backend did not claim one" (silent).
                    continue
                if not isinstance(inner, dict):
                    shape = f"type='content' inner=<{type(inner).__name__}>"
                else:
                    inner_type = inner.get("type")
                    if isinstance(inner_type, str) and inner_type in _KNOWN_CONTENT_BLOCK_TYPES:
                        continue
                    shape = (
                        f"type='content' inner_type={_rendered_shape_type(inner_type)} "
                        f"inner_keys={_rendered_key_names(inner)}"
                    )
            else:
                shape = (
                    f"type={_rendered_shape_type(entry_type)} " f"keys={_rendered_key_names(entry)}"
                )
        shapes.append(shape)
        budget -= len(shape) + 2
    return redact_text("; ".join(shapes))[:_MAX_SHAPE_DIAGNOSTIC] if shapes else ""


def log_unrenderable_content(log: logging.Logger, tool_use_id: Any, content: Any) -> None:
    """Emit the "this tool shows no output" warning, once, for both drop sites.

    The two tool-result parsers (:func:`_build_tool_result_event` here and
    ``AcpClient._extract_tool_call_update``) reach the same dead end, and this PR
    exists because their leniency had drifted apart. Shipping the warning as two
    copies would recreate exactly that: the message, the redaction, the ``%r``
    escaping and the shape call all have to stay identical, and nothing but
    convention would keep them so.

    ``log`` is passed in rather than taken from this module so each parser's
    records still carry ITS OWN logger name -- callers filter on that.
    """
    shapes = unrenderable_content_shapes(content)
    if not shapes:
        return
    log.warning(
        "tool_call_update %s: no content entry could be rendered, so this tool "
        "shows no output at all. Unrecognised entry shapes: %s. ACP expects "
        "{'type': 'content', 'content': {'type': 'text', 'text': ...}}; a bare "
        "{'type': 'text', 'text': ...} block is also read. Shapes only -- entry "
        "VALUES are withheld from this log.",
        _loggable_request_id(tool_use_id),
        shapes,
    )


def _measure_tool_output(redacted: str) -> tuple[str, int]:
    """The full redacted output's digest and byte count, or "not recorded".

    Measured BEFORE the display cut, because the cut is for the dashboard and a
    digest of a truncated prefix would state a fact about text nothing observed:
    two outputs sharing their first bounded characters would hash the same, and
    the byte count would under-report every output past the bound.

    Computed only while the crew log is on. The pair has one consumer, the emitter,
    which is gated on the same flag -- so with the flag off this is work nothing
    reads, over text as large as a file the model just printed. ``("", -1)`` is
    the emitter's own spelling for a field it must not record, distinct from a
    measurement of empty output, so the off path records neither field rather
    than recording zero.

    One function for both parser sites (this module's terminal-frame builder and
    the streaming update parser in ``client``) so the digest they produce cannot
    drift apart: a crew log reader comparing two entries has no way to tell which
    parser produced either one.
    """
    from kiro_crew.crew_log.emit import enabled as _crew_log_enabled

    if not _crew_log_enabled():
        return "", -1
    raw = redacted.encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _build_tool_result_event(update: dict[str, Any], cache_scope: str = "") -> AcpEvent | None:
    """Build an ``EVENT_TOOL_RESULT`` from output or a terminal tool status.

    Three output shapes: ``content[].content.text`` blocks (stream mid-turn),
    ``rawOutput.items[]`` (``Text`` / ``Json.stdout``) on ``status=completed``,
    and -- since ``rawOutput`` is unstructured passthrough rather than a
    contract -- any other non-empty ``rawOutput`` object, serialised. A terminal
    update with no renderable output emits the observed status without inventing
    output. Returns None when the update carries neither output nor terminal
    status (refinement-only updates are handled by
    :func:`_build_tool_refinement_event`).

    ``cache_scope`` is the emitting session's origin scope, forwarded only so the
    duration histogram closes the same registry entry its start opened.
    """
    tool_use_id = update.get("toolCallId", "")
    if not tool_use_id:
        return None
    # A terminal status is useful even when output parsing finds no text: it
    # closes the observed round-trip without claiming a result body. A
    # non-terminal status leaves the clock running for the real completion.
    record_tool_call_finished(tool_use_id, status=update.get("status"), scope=cache_scope)
    # Parts are collected RAW and redaction runs once over their JOIN, before
    # the single 8000-char bound. Both orderings matter: bounding first can
    # split a credential at a cut into fragments no redaction regex matches,
    # and redacting per part would blind the multi-line PEM pattern
    # (``BEGIN ... [\s\S]*? END``) to a key whose header and footer arrive in
    # DIFFERENT parts — only the combined text shows such a secret whole. The
    # former per-part 4000 cut is deliberately gone: applied before redaction
    # it IS this defect class, and it cannot be reconstructed afterwards, so
    # the final head cut is the one bound.
    output_parts: list[str] = []
    # Path 1: content blocks (mid-stream).
    content = update.get("content")
    if isinstance(content, list):
        for block in content:
            text = tool_call_content_text(block)
            if text:
                output_parts.append(text)
    # Path 2: rawOutput (status=completed) fallback.
    if not output_parts:
        raw_output = update.get("rawOutput")
        if isinstance(raw_output, dict):
            items = raw_output.get("items", [])
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if "Text" in item and item.get("Text"):
                        output_parts.append(str(item["Text"]))
                        continue
                    j = item.get("Json")
                    if isinstance(j, dict):
                        if "stdout" in j and j.get("stdout"):
                            output_parts.append(str(j["stdout"]))
                        else:
                            _mcp_text = _mcp_content_text(j)
                            if _mcp_text is not None:
                                output_parts.append(_mcp_text)
                            else:
                                output_parts.append(_dumps_degraded(j, default=str))
            # Path 3: an object that is not that envelope at all. ``rawOutput``
            # is unstructured passthrough, so ``items[]`` is ONE producer's
            # private wrapper rather than a contract, and an object Crew does
            # not recognise is not evidence the tool produced no output.
            # Treating it as absent returns None from this builder, which drops
            # the whole EVENT_TOOL_RESULT -- the event that writes both
            # ``meta["output"]`` and ``meta["done"]`` for the pill. The Output
            # tab is lost outright; ``done`` survives only if assistant text
            # follows the tool group, because _on_text_chunk (dashboard/chat_turn/stream_chunks.py)
            # then sweeps unmarked tool rows done. Serialise it instead,
            # which is already the policy one level in: an unrecognised ``Json``
            # envelope is dumped rather than dropped (above), and
            # ``parse_todo_snapshot`` likewise accepts a bare-dict ``rawOutput``.
            # Gated on the ABSENCE of ``items`` so no ``items[]`` envelope
            # changes here, including one that legitimately yields nothing. No
            # ``not output_parts`` test is needed: this whole branch already runs
            # only when Path 1 found nothing, which is what keeps a content block
            # winning over the raw envelope.
            if raw_output and "items" not in raw_output:
                # Codex returns the MCP response inside result/error. Preserve
                # its text framing; attribution still comes from the call.
                mcp_result = raw_output.get("result")
                mcp_text = (
                    _mcp_content_text(mcp_result)
                    if isinstance(mcp_result, dict) and raw_output.get("error") is None
                    else None
                )
                output_parts.append(
                    mcp_text if mcp_text is not None else _dumps_degraded(raw_output, default=str)
                )
    tool_status = str(update.get("status") or "")
    if not output_parts:
        if tool_status not in TERMINAL_TOOL_STATUSES:
            log_unrenderable_content(logger, tool_use_id, content)
            return None
        return AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id=tool_use_id,
            tool_final=tool_status == "completed",
            tool_status=tool_status,
        )
    joined = "\n".join(output_parts)
    _redacted = _redact(joined)
    # Measured only when the crew log is on. These two fields have exactly one
    # consumer -- the flag-gated emitter -- so hashing every tool result while it
    # is off is work nothing reads, and a tool result is as large as a file the
    # model just printed. The defaults carry "not recorded" rather than a
    # measurement of nothing, which is the distinction the emitter already keeps.
    tool_output_digest, tool_output_bytes = _measure_tool_output(_redacted)
    final_output = _redacted[: session_directive.MAX_TOOL_RESULT_CHARS]
    # Both session-directive sentinels are TAIL-anchored, and this cut runs AFTER
    # redaction -- which can grow the text, since a credential is replaced by a
    # longer placeholder. So a producer bounding its own length cannot guarantee
    # the marker survives here; re-attach it when the cut removed it, exactly as
    # the App render marker below is re-injected at this same seam.
    final_output = session_directive.preserve_tail_marker(_redacted, final_output)
    # An MCP App render marker lives at offset 0 of its own text part, but the
    # 8000-char join cut is applied to the CONCATENATION of all parts: when the
    # marker part is preceded by other (up to 4000-char) parts, its offset in
    # the joined string can exceed 8000 and the slice drops it, so
    # ``mcp_apps_render.find_marker`` never sees it and the app never mounts.
    # If the pre-slice text carried a marker that the slice removed, re-inject
    # it at offset 0 so it stays under any cut and remains detectable. The
    # marker is a fixed control token, not sensitive, so it needs no redaction.
    marker_match = mcp_apps_render.MARKER_RE.search(joined)
    if marker_match and mcp_apps_render.find_marker(final_output) is None:
        final_output = f"{marker_match.group(0)} {final_output}"
    return AcpEvent(
        kind=EVENT_TOOL_RESULT,
        tool_call_id=tool_use_id,
        tool_output=final_output,
        tool_output_digest=tool_output_digest,
        tool_output_bytes=tool_output_bytes,
        # Only a result the redactor changed can hold a credential worth a
        # fingerprint, so an ordinary result pays nothing extra.
        tool_output_credentials=tool_output_fingerprints(joined) if _redacted != joined else (),
        tool_final=update.get("status") == "completed",
        tool_status=str(update.get("status") or ""),
    )


def _todo_payload(raw_output: Any) -> dict[str, Any] | None:
    """Dig the todo dict out of ``rawOutput``, tolerating shape drift.

    kiro-cli wraps it as ``{"items": [{"Json": {...}}]}``, but the wrapper is an
    internal detail we do not control, so a bare dict and a bare list of
    candidates are both accepted. Returns the first mapping that actually
    carries a ``tasks`` list — never a partially-matched shell.
    """
    candidates: list[Any] = []
    if isinstance(raw_output, dict):
        items = raw_output.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    # {"Json": {...}} wrapper, else the item itself.
                    candidates.extend(v for v in item.values() if isinstance(v, dict))
                    candidates.append(item)
        candidates.append(raw_output)
    elif isinstance(raw_output, list):
        candidates.extend(raw_output)
    for cand in candidates:
        if isinstance(cand, dict) and isinstance(cand.get("tasks"), list):
            return cand
    return None


def parse_todo_snapshot(
    update: dict[str, Any],
    tool_name_cache: dict[str, str] | None = None,
    cache_scope: str = "",
) -> dict[str, Any] | None:
    """Normalise a ``todo_list`` tool result into a UI-ready snapshot.

    Returns ``{description, tasks: [{id, text, completed}]}`` or None when this
    update is not a todo_list result. EVERY todo_list command (create / complete
    / list) echoes the entire list, so the return value is always a full
    snapshot — callers replace their stored copy rather than merging.

    An empty ``tasks`` list is a MEANINGFUL result (the agent cleared its list),
    so it returns a snapshot with zero tasks rather than None. Only a genuine
    non-match or unparseable payload yields None.

    ``tool_name_cache`` / ``cache_scope`` are the caller's own per-tool caches,
    passed the way :func:`_build_tool_refinement_event` takes them, and they are
    what identifies the call: see the fallback below.
    """
    if not isinstance(update, dict):
        return None
    # kiro-cli puts ``_meta`` on the ``tool_call`` frame ALONE. The result frame
    # this parses carries none (captured in test/fixtures/acp_frames/kiro/
    # session.jsonl), so asking THAT frame for its identity answers "" and the
    # snapshot is dropped on every live call, taking the task panel and the crew
    # log's plan/updated entry with it. So fall back to the name the preceding
    # tool_call frame cached under this call id, the same recovery
    # build_permission_event makes for the permission frame, which carries no
    # _meta for the same reason.
    #
    # A frame that DOES assert an identity is believed as it stands: the cache
    # only answers for a frame that asserts nothing. Read under
    # scoped_tool_cache_key because these entries are origin-bound, so a
    # backend-internal child replaying a parent's id reads nothing.
    tool_name = _kiro_tool_name(update)
    if not tool_name and tool_name_cache is not None:
        call_id = update.get("toolCallId") or ""
        if isinstance(call_id, str) and call_id:
            tool_name = tool_name_cache.get(scoped_tool_cache_key(cache_scope, call_id)) or ""
    if tool_name != KIRO_TOOL_TODO_LIST:
        return None
    payload = _todo_payload(update.get("rawOutput"))
    if payload is None:
        return None
    tasks: list[dict[str, Any]] = []
    for idx, raw in enumerate(payload.get("tasks") or []):
        if not isinstance(raw, dict):
            continue
        text = raw.get("task_description") or raw.get("description") or raw.get("text") or ""
        if not isinstance(text, str):
            text = str(text)
        text = _redact(text)[:TODO_TEXT_MAX]
        task_id = raw.get("id")
        tasks.append(
            {
                # Provider-authored like the text: redacted BEFORE the bound, so
                # the cut cannot sever a secret and leak its head.
                "id": _redact(str(task_id) if task_id is not None else str(idx + 1))[:TODO_ID_MAX],
                "text": text,
                # `completed` is a plain bool in kiro-cli 2.14.0 — there is no
                # in-progress state. bool() keeps a stray truthy string from
                # reaching the UI as a non-boolean.
                "completed": bool(raw.get("completed")),
            }
        )
        if len(tasks) >= TODO_TASKS_MAX:
            break
    description = payload.get("description") or ""
    if not isinstance(description, str):
        description = str(description)
    return {
        "description": _redact(description)[:TODO_TEXT_MAX],
        "tasks": tasks,
    }
