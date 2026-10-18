"""The ``session/update`` parser both transports route through.

Message and thought chunk text with its ``messageId``, the ``tool_call`` and
refinement event builders that write the caller-owned provenance caches, and
:func:`parse_session_update`, which dispatches one update over them and the
tool-result owner.

:mod:`kiro_crew.acp._dispatch` stays the import path and the patch surface; see
:mod:`kiro_crew.acp._dispatch_parts`.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp._dispatch_parts.surfaced_text import (
    _dumps_degraded,
    _redact,
    derive_edit_diff,
    extract_tool_purpose,
    make_unified_diff,
    select_tool_title,
)
from kiro_crew.acp._dispatch_parts.tool_call_identity import classify_tool_call, harness_tool_name
from kiro_crew.acp._dispatch_parts.tool_call_provenance import (
    _params_chars_for_admission,
    admit_tool_call_cache_entry,
    scoped_tool_cache_key,
)
from kiro_crew.acp._dispatch_parts.tool_result_events import (
    _build_tool_result_event,
    parse_todo_snapshot,
)
from kiro_crew.acp.types import (
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TODO_UPDATE,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_TOOL_CALL,
    UPDATE_TOOL_CALL_UPDATE,
    AcpEvent,
)
from kiro_crew.metrics.tool_calls import note_tool_call_started
from kiro_crew.platform.tool_paths import WRITE_PLANE_KINDS

# ── session/update parser ───────────────────────────────────────────────────
#
# Single source of truth for turning one ``session/update`` notification's inner
# ``update`` dict into ``AcpEvent``s. BOTH ``AcpClient`` and ``AcpRuntime`` route
# through this so they cannot drift on frame shape (kiro-cli 2.10.0 nests chunk
# text under ``content.text`` rather than a flat ``text`` field).
#
# The parser is PURE except for the optional caller-owned ``tool_input_cache``
# dict it may write (the ``toolCallId -> redacted input`` map each class keeps so
# a later tool result can recover the originating input). All redaction of
# LLM-influenced fields (titles, inputs, purposes, outputs) happens in this parser,
# through the helpers in ``surfaced_text``, so tool data is never surfaced
# unredacted. Stats and stale/stall bookkeeping stay
# per-class: the caller walks the returned events.


def parse_text_chunk(update: dict[str, Any]) -> tuple[str | None, bool]:
    """Extract text from an ``agent_message_chunk`` / ``agent_thought_chunk`` update.

    Returns ``(text_or_None, is_thinking)``. kiro-cli 2.10.0 nests the text under
    ``content`` (``{type, text}``); a flat top-level ``text`` is accepted as a
    back-compat fallback for older kiro. ``is_thinking`` is True for a thought
    chunk, or when an ``agent_message_chunk``'s inner ``content.type`` is a
    reasoning type.
    """
    kind = update.get("sessionUpdate")
    content = update.get("content")
    content = content if isinstance(content, dict) else {}
    text = content.get("text") or update.get("text")
    text_val = str(text) if text else None
    if kind == UPDATE_AGENT_MESSAGE_CHUNK:
        content_type = content.get("type", "text")
        is_thinking = content_type in ("thinking", "reasoning")
        return text_val, is_thinking
    if kind == UPDATE_AGENT_THOUGHT_CHUNK:
        return text_val, True
    return None, False


# A real id is well under this (claude's are ~60 chars); anything longer is not
# one and is not worth persisting onto every transcript row.
_MESSAGE_ID_MAX_CHARS = 256


def parse_message_id(update: object) -> str:
    """The ``messageId`` a message/thought chunk update carries, or ``""``."""
    if not isinstance(update, dict):
        return ""
    message_id = update.get("messageId")
    if not isinstance(message_id, str) or len(message_id) > _MESSAGE_ID_MAX_CHARS:
        return ""
    return message_id


def _build_tool_call_event(
    update: dict[str, Any],
    tool_input_cache: dict[str, str] | None,
    shell_cache: dict[str, bool] | None = None,
    raw_params_cache: dict[str, dict] | None = None,
    mcp_server_name_cache: dict[str, str] | None = None,
    tool_name_cache: dict[str, str] | None = None,
    cache_scope: str = "",
    tool_input_redacted_cache: dict[str, bool] | None = None,
    diff_path_cache: dict[str, str] | None = None,
    harness_tool_name_cache: dict[str, str] | None = None,
    tool_kind_cache: dict[str, str] | None = None,
    cache_overflow: set[str] | None = None,
    identity_unreadable_cache: dict[str, bool] | None = None,
) -> AcpEvent:
    """Build an ``EVENT_TOOL_CALL`` from a ``tool_call`` update (with redaction)."""
    title = update.get("title", "unknown")
    _wire_title = title if isinstance(title, str) and title != "unknown" else ""
    kind = update.get("kind", "unknown")
    # First PRESENT key, not first truthy one: an explicit empty ``rawInput``
    # (``reset_conversation({})``, ``resource_status({})``) is a real argument
    # set, and the out-of-band directive claim digests it. An ``or`` chain
    # collapsed ``{}`` to None, so a no-argument directive recorded no digest
    # and its parked record was never claimed.
    raw_input = next(
        (update[k] for k in ("rawInput", "input", "params") if update.get(k) is not None),
        None,
    )
    purpose = extract_tool_purpose(raw_input)
    _raw_tool_call_id = update.get("toolCallId", "")
    tool_call_id = _raw_tool_call_id if isinstance(_raw_tool_call_id, str) else ""
    # ORIGIN-BOUND cache key (see build_permission_event): entries written
    # here are readable only under the same emitting-session scope.
    _ck = scoped_tool_cache_key(cache_scope, tool_call_id)
    # ONE admission decision for every sibling store (see
    # ``admit_tool_call_cache_entry``): a refused id is written to none of them,
    # so the cache handles below are simply absent for this frame. The event
    # itself is still built and displayed; only the retained provenance is
    # withheld, and the permission request for the id is then refused by the
    # handle as unverifiable. The rendered input is computed here, before the
    # gate, in its FINAL retained form -- diff content block or derived edit
    # diff over the raw params, then redaction -- because its size is one of
    # the two bounds the gate applies, and a bound measured on an intermediate
    # rendering (the params alone) would let a small ``rawInput`` beside a large
    # diff block pass admission and then retain the diff unmeasured.
    input_str = ""
    if tool_call_id and raw_input:
        input_str = (
            _dumps_degraded(raw_input, indent=2)
            if isinstance(raw_input, (dict, list))
            else str(raw_input)
        )
    # The params rendering's size stands for the ``raw_params`` dict the row
    # retains alongside the final rendering: a diff block or derived diff
    # REPLACES ``input_str`` below with something usually smaller than the
    # params it came from, so measuring the final string alone would admit a
    # near-limit ``rawInput`` beside a small diff and retain the dict unmeasured.
    # A rendering the encoder degraded is no measurement at all and counts as
    # over the bound (``_params_chars_for_admission``).
    _params_chars = _params_chars_for_admission(input_str)
    # Edit tools with diff content blocks → render a unified diff instead.
    # Also capture oldText/path for the file-change snapshot (race-free source).
    found_diff = False
    _diff_old_text: str | None = None
    _diff_path: str = ""
    content_blocks = update.get("content", [])
    if isinstance(content_blocks, list):
        for cb in content_blocks:
            if isinstance(cb, dict) and cb.get("type") == "diff":
                _cb_old = cb.get("oldText")
                _diff_old_text = _cb_old if isinstance(_cb_old, str) else (_cb_old or "")
                _cb_path = cb.get("path")
                _diff_path = _cb_path if isinstance(_cb_path, str) else ""
                diff_str = make_unified_diff(
                    _diff_old_text or "", cb.get("newText") or "", _diff_path
                )
                if diff_str:
                    input_str = diff_str
                    found_diff = True
                break
    # Fallback when no diff content block was present: derive from the edit
    # args themselves (strReplace pair, create/insert content). Gated on the
    # EDIT kind — "content"-shaped args exist on many non-edit tools, and a
    # derived diff would corrupt their input display.
    if not found_diff and (
        kind == "edit" or (isinstance(raw_input, dict) and raw_input.get("command") == "strReplace")
    ):
        diff_str = derive_edit_diff(raw_input)
        if diff_str:
            input_str = diff_str
    input_redacted = False
    if input_str:
        safe_input = _redact(input_str)
        input_redacted = safe_input != input_str
        input_str = safe_input
    # The admission runs only for a caller that hands an overflow record: the
    # record is what the refusal is written to, and a harness whose handle
    # hands none (every non-KAS harness, H13) retains as it always did.
    if (
        tool_call_id
        and cache_overflow is not None
        and not admit_tool_call_cache_entry(
            _ck,
            cache_scope,
            max(_params_chars, len(input_str)),
            cache_overflow,
            raw_params_cache,
            shell_cache,
            mcp_server_name_cache,
            tool_name_cache,
            diff_path_cache,
            tool_kind_cache,
            tool_input_cache,
            tool_input_redacted_cache,
            identity_unreadable_cache,
            harness_tool_name_cache,
            path_chars=len(_diff_path),
        )
    ):
        raw_params_cache = shell_cache = mcp_server_name_cache = tool_name_cache = None
        diff_path_cache = tool_kind_cache = tool_input_cache = tool_input_redacted_cache = None
        identity_unreadable_cache = harness_tool_name_cache = None
    # Cache the STRUCTURED raw params (dict) keyed by toolCallId so a later
    # permission_request — which carries only a truncated title — can recover
    # them for governance enforcement (raw_tool_params). Mirrors AcpClient's
    # _tool_call_params. shell_cache/tool_input_cache below serve display/is_shell.
    # Truthy-gated on purpose, unlike the event's own ``raw_tool_params`` below:
    # this cache is the permission event's TRUSTED params source, and an empty
    # dict here would earn ``raw_params_trusted`` for a call whose arguments the
    # refinement has not streamed yet (claude-agent-acp sends ``{}`` first). The
    # directive digest reads the event field, not this cache.
    if tool_call_id and raw_params_cache is not None and isinstance(raw_input, dict) and raw_input:
        raw_params_cache[_ck] = raw_input
    # Capture the shell signal from the RAW kind (before redaction) so a later
    # permission_request (which carries no kind) can inherit it via shell_cache.
    # Cache ONLY when the update actually carried a usable kind: a missing kind
    # defaults to "unknown"/non-shell for THIS event's display, but caching that
    # False would let the later permission event read it as a RESOLVED non-shell
    # classification (shell_classified) and skip the low-fidelity downgrade
    # without any classification having actually happened.
    #
    # The classification reads the WHOLE frame (kind + adapter-authored MCP
    # markers + the harness's own ``_meta`` shell channel), not the kind alone:
    # codex-acp reports MCP calls as kind="execute", and caching that as shell
    # sends every codex MCP call into the deny-by-default shell gate with no
    # command to verify; goose sends its builtin shell with no ``kind`` at all,
    # and only its ``_meta`` pair says "this is a command".
    identity = classify_tool_call(update)
    is_shell = identity.is_shell
    if tool_call_id and shell_cache is not None and identity.kind_resolved:
        shell_cache[_ck] = is_shell
    # Capture the TRUSTED MCP server identity (_meta.kiro.mcpServerName, or the
    # codex marker's adapter-resolved server) so the later permission_request —
    # the dashboard's gate path, which carries no _meta — can inherit it via
    # mcp_server_name_cache. This is what lets the app-own-server auto-approve
    # (hooks.on_tool_call) fire on the permission path: without the cache, the
    # permission event's mcp_server_name is always "" and the branch never
    # matches.
    _mcp_server_name = identity.mcp_server_name
    if tool_call_id and mcp_server_name_cache is not None:
        mcp_server_name_cache[_ck] = _mcp_server_name
    # Round-trip clock for kirocrew.tool.call.duration. Placed after the trusted
    # MCP identity is resolved so an MCP call is classified by its transport
    # rather than by the kind it reported; the terminal status is stamped in
    # _build_tool_result_event. Keyed by the SAME origin scope as the caches
    # above, because one runtime hosts many sessions and a backend-assigned
    # toolCallId is unique only within one of them. Idempotent per scoped id, so
    # the tool_call_update refinements that follow cannot restart the clock.
    note_tool_call_started(
        tool_call_id, kind=kind, mcp_server_name=_mcp_server_name, scope=cache_scope
    )
    # Same lifecycle for the trusted tool name so the permission event can
    # reconstruct the canonical mcp__<server>__<tool> for per-tool governance in
    # the app-own-server auto-approve.
    _tool_name = identity.tool_name
    if tool_call_id and tool_name_cache is not None:
        tool_name_cache[_ck] = _tool_name
    # The tool's own name from this first frame, for the permission event's
    # harness_tool_id on a backend with no _meta.kiro.toolId. Written on every
    # tool_call, "" included, so a reused id never inherits a stale name. A
    # sibling of the caches above under the same admission: the handle is None
    # here for a frame the gate refused, the value is bounded at construction
    # (``harness_tool_name``), and the handle releases the row at the terminal.
    if tool_call_id and harness_tool_name_cache is not None:
        harness_tool_name_cache[_ck] = harness_tool_name(update)
    # A frame that PRESENTED an identity half the bound refused is recorded as
    # such (a bool, never the half), so the permission request that follows is
    # refused rather than judged under the empty name the caches above hold.
    # Sparse: a row exists only for a frame that presented an unreadable half,
    # and a tool_call frame OVERWRITES the verdict under its id the way the
    # identity caches beside it are overwritten, so a mark never outlives the
    # frame that earned it. Keyed like its siblings, evicted/released with them.
    if tool_call_id and identity_unreadable_cache is not None:
        if identity.identity_unreadable:
            identity_unreadable_cache[_ck] = True
        else:
            identity_unreadable_cache.pop(_ck, None)
    # Cache the content block's path for the permission event (see
    # build_permission_event). Written only when a diff block NAMED a path, so
    # a later frame without one cannot clobber a real target with "". The path
    # was read above with the rendering, before the admission gate.
    if tool_call_id and _diff_path and diff_path_cache is not None:
        diff_path_cache[_ck] = _diff_path
    # Cache the harness-classified ``kind`` for the permission event. KAS's
    # ``session/request_permission`` carries a ``toolCall`` of id, status and
    # title only -- no ``kind`` (recorded: ``test/fixtures/acp_frames/kas/
    # session.jsonl``) -- so without this the gate's write-plane delete routing
    # saw ``""`` for every KAS call. BOUNDED AT RETENTION: the frame's ``kind``
    # is an externally-sized string, and only membership in the closed
    # write-plane vocabulary is retained -- the stored value is one of the
    # ``WRITE_PLANE_KINDS`` literals, never the frame's own bytes, so an entry
    # costs a fixed few bytes whatever the frame carried. Any other kind
    # (``read``/``search``/``execute``/``fetch``/``other``, the placeholder
    # ``"unknown"``, a non-string) is not written: the reader carries nothing
    # else, so retaining it would be growth with no reader. The entry COUNT is
    # the sibling caches' -- one per write-plane tool_call under the same
    # origin-scoped id, cleared per turn with them -- and is not capped here on
    # its own: an entry this cache refused would reach the gate as a kindless
    # delete and route nowhere, so a count cap on this store alone would fail
    # OPEN, and the per-turn population is already what bounds the params
    # cache beside it.
    if tool_call_id and tool_kind_cache is not None:
        if isinstance(kind, str) and kind in WRITE_PLANE_KINDS:
            tool_kind_cache[_ck] = kind
    # The rendered input retained here is the string the gate above measured:
    # no rendering happens between the two.
    if tool_call_id and input_str and tool_input_cache is not None:
        tool_input_cache[_ck] = input_str
        if tool_input_redacted_cache is not None:
            tool_input_redacted_cache[_ck] = input_redacted
    if purpose:
        purpose = _redact(purpose)
    title = select_tool_title(title, raw_input, kind, is_shell=is_shell) or ""
    if title:
        title = _redact(title)
    if kind:
        kind = _redact(kind)
    return AcpEvent(
        kind=EVENT_TOOL_CALL,
        title=title,
        wire_title=_wire_title,
        tool_kind=kind,
        tool_purpose=purpose,
        tool_input=input_str,
        tool_input_redacted=input_redacted,
        tool_call_id=tool_call_id,
        raw_tool_params=raw_input if isinstance(raw_input, dict) else None,
        is_shell=is_shell,
        # Trusted identity from the adapter-authored markers (NOT the
        # LLM-authored title) -- see classify_tool_call for the sources.
        tool_name=_tool_name,
        mcp_server_name=_mcp_server_name,
        # Earned only when an identity pair was actually extracted from such a
        # source: a frame with no marker populates nothing and asserts no
        # provenance.
        tool_identity_trusted=identity.tool_identity_trusted,
        mcp_identity_trusted=identity.identity_trusted,
        diff_old_text=_diff_old_text,
        diff_path=_diff_path,
    )


def _build_tool_refinement_event(
    update: dict[str, Any],
    tool_input_cache: dict[str, str] | None,
    shell_cache: dict[str, bool] | None = None,
    raw_params_cache: dict[str, dict] | None = None,
    cache_scope: str = "",
    tool_input_redacted_cache: dict[str, bool] | None = None,
    diff_path_cache: dict[str, str] | None = None,
    cache_overflow: set[str] | None = None,
    mcp_server_name_cache: dict[str, str] | None = None,
    tool_name_cache: dict[str, str] | None = None,
    tool_kind_cache: dict[str, str] | None = None,
    identity_unreadable_cache: dict[str, bool] | None = None,
    harness_tool_name_cache: dict[str, str] | None = None,
) -> AcpEvent | None:
    """Build an ``EVENT_TOOL_CALL_UPDATE`` (refined title/kind/input) for a tool.

    ``mcp_server_name_cache`` / ``tool_name_cache`` / ``tool_kind_cache`` are
    never written by a refinement (identity and kind come from the initial
    frame); they are passed so the ONE admission sees the whole population and a
    refused refinement evicts the id's row from every sibling store, not only the
    ones a refinement writes.
    claude-agent-acp emits a follow-up ``tool_call_update`` once the streamed
    ``rawInput`` is complete (the initial ``tool_call`` had empty input + a
    generic title). Returns None when the update carries no refinement fields
    (pure-output updates are handled by :func:`_build_tool_result_event`).
    """
    tool_use_id = update.get("toolCallId", "")
    if not tool_use_id or not isinstance(tool_use_id, str):
        return None
    # ORIGIN-BOUND cache key (see build_permission_event).
    _rk = scoped_tool_cache_key(cache_scope, tool_use_id)
    title = update.get("title")
    kind = update.get("kind")
    raw_input = update.get("rawInput")
    if title is None and kind is None and not raw_input:
        return None
    input_str = ""
    if isinstance(raw_input, (dict, list)) and raw_input:
        # _dumps_degraded keeps this site's pre-existing (TypeError, ValueError)
        # -> str(raw_input) degrade and adds the RecursionError arm that the
        # old except list here missed (RecursionError is a RuntimeError).
        input_str = _dumps_degraded(raw_input, indent=2)
    elif isinstance(raw_input, str):
        input_str = raw_input
    # Stands for the retained ``raw_params`` dict, as in the initial tool_call:
    # the diff block below replaces the rendering with a usually smaller string,
    # and a degraded rendering counts as over the bound.
    _params_chars = _params_chars_for_admission(input_str)
    # Rendered in its FINAL retained form before the gate, as the initial
    # tool_call does: the diff content block (where claude-agent-acp first
    # carries it) replaces the params rendering, then redaction, and THAT is
    # the string the size bound measures and the cache retains.
    content_blocks = update.get("content", [])
    _diff_old_text: str | None = None
    _diff_path: str = ""
    if isinstance(content_blocks, list):
        for cb in content_blocks:
            if isinstance(cb, dict) and cb.get("type") == "diff":
                _cb_old = cb.get("oldText")
                _diff_old_text = _cb_old if isinstance(_cb_old, str) else (_cb_old or "")
                _cb_path = cb.get("path")
                _diff_path = _cb_path if isinstance(_cb_path, str) else ""
                diff_str = make_unified_diff(
                    _diff_old_text or "", cb.get("newText") or "", _diff_path
                )
                if diff_str:
                    input_str = diff_str
                break
    input_redacted = False
    if input_str:
        safe_input = _redact(input_str)
        input_redacted = safe_input != input_str
        input_str = safe_input
    # Same single admission as the initial tool_call, and for the same callers
    # (an overflow record opts a harness in): a refinement of an id the caches
    # already hold is admitted unless its payload is over the size bound; a
    # first sighting past the shared count cap is written nowhere, and a
    # refused refinement evicts the id from every sibling store.
    if cache_overflow is not None and not admit_tool_call_cache_entry(
        _rk,
        cache_scope,
        max(_params_chars, len(input_str)),
        cache_overflow,
        raw_params_cache,
        shell_cache,
        mcp_server_name_cache,
        tool_name_cache,
        diff_path_cache,
        tool_kind_cache,
        tool_input_cache,
        tool_input_redacted_cache,
        identity_unreadable_cache,
        harness_tool_name_cache,
        path_chars=len(_diff_path),
    ):
        raw_params_cache = shell_cache = diff_path_cache = None
        tool_input_cache = tool_input_redacted_cache = None
    # Same diff-block path cache as the initial tool_call.
    if _diff_path and diff_path_cache is not None:
        diff_path_cache[_rk] = _diff_path
    if input_str and tool_input_cache is not None:
        tool_input_cache[_rk] = input_str
        if tool_input_redacted_cache is not None:
            tool_input_redacted_cache[_rk] = input_redacted
    # The refinement's rawInput is the COMPLETE params object — cache it for
    # the permission event's structured-params (path/arg scope) checks, same
    # as the initial tool_call does. Without this, a backend whose initial
    # tool_call streams empty rawInput leaves raw_params_cache forever empty
    # and every child permission request downgrades to low fidelity.
    if raw_params_cache is not None and isinstance(raw_input, dict) and raw_input:
        raw_params_cache[_rk] = raw_input
    # Refresh the cached shell signal only when this refinement carries a kind
    # (kind is optional on updates); a kind-less refinement must not clobber a
    # True cached by the initial tool_call. Mirrors AcpClient exactly. Resolved
    # BEFORE the title so the label rule sees the real classification rather
    # than a missing kind.
    _identity = classify_tool_call(update)
    if shell_cache is not None:
        if _identity.kind_resolved:
            shell_cache[_rk] = _identity.is_shell
        is_shell = shell_cache.get(_rk, False)
    else:
        is_shell = _identity.is_shell if _identity.kind_resolved else False
    # A refinement carrying a title but no rawInput still overwrites the pill,
    # so the command has to be recoverable from the params the initial
    # tool_call cached — otherwise a backend that sends a generic title on both
    # events lands that label on a pill the first event got right.
    _title_params: object = raw_input
    if not (isinstance(raw_input, dict) and raw_input) and raw_params_cache is not None:
        _title_params = raw_params_cache.get(_rk)
    title_source = select_tool_title(title, _title_params, kind, is_shell=is_shell)
    title_str = _redact(title_source) if title_source else ""
    kind_str = _redact(kind) if isinstance(kind, str) and kind else ""
    # The refinement's rawInput is the COMPLETE params object, so it carries the
    # reserved purpose argument too. Read it here or the purpose is lost on every
    # backend whose initial tool_call streams an empty rawInput — and consumers
    # that treat an empty purpose as "fall back to the raw title" (the session
    # list's running-status line) would replace a good purpose with a command.
    purpose = extract_tool_purpose(raw_input)
    if purpose:
        purpose = _redact(purpose)
    return AcpEvent(
        kind=EVENT_TOOL_CALL_UPDATE,
        title=title_str,
        wire_title=title if isinstance(title, str) else "",
        tool_kind=kind_str,
        tool_purpose=purpose,
        tool_input=input_str,
        tool_input_redacted=input_redacted,
        tool_call_id=tool_use_id,
        raw_tool_params=raw_input if isinstance(raw_input, dict) else None,
        is_shell=is_shell,
        diff_old_text=_diff_old_text,
        diff_path=_diff_path,
    )


def parse_session_update(
    update: dict[str, Any],
    *,
    tool_input_cache: dict[str, str] | None = None,
    shell_cache: dict[str, bool] | None = None,
    raw_params_cache: dict[str, dict] | None = None,
    mcp_server_name_cache: dict[str, str] | None = None,
    tool_name_cache: dict[str, str] | None = None,
    cache_scope: str = "",
    tool_input_redacted_cache: dict[str, bool] | None = None,
    diff_path_cache: dict[str, str] | None = None,
    harness_tool_name_cache: dict[str, str] | None = None,
    tool_kind_cache: dict[str, str] | None = None,
    cache_overflow: set[str] | None = None,
    identity_unreadable_cache: dict[str, bool] | None = None,
) -> list[AcpEvent]:
    """Parse one ``session/update`` inner ``update`` dict into ``AcpEvent``s.

    Single source of truth shared by ``AcpClient`` and ``AcpRuntime``. Returns a
    list (0–2 events) so a ``tool_call_update`` can yield BOTH a result and a
    refinement in the same order the legacy client emitted them (result first).
    ``usage_update`` is NOT an event — use :func:`parse_usage_update`.

    ``tool_input_cache`` (caller-owned) is written with ``toolCallId -> redacted
    input`` for ``tool_call`` / refinement updates, mirroring each class's
    ``_tool_call_inputs`` map. Stats and stall bookkeeping stay with the caller.
    ``cache_overflow`` (caller-owned, same lifecycle) OPTS THE CALLER INTO the
    cache admission bound and collects the scope digests whose frames it refused
    this turn (see :func:`admit_tool_call_cache_entry`). A caller that hands
    ``None`` -- every harness outside ``ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL``,
    through ``AcpSessionHandle._tool_call_cache_overflow_for_wire`` -- retains as
    it always did; the bound is the KAS projection's, not a change to the
    first-class harness's construction (H13).
    """
    if not isinstance(update, dict):
        return []
    kind = update.get("sessionUpdate")
    events: list[AcpEvent] = []
    if kind in (UPDATE_AGENT_MESSAGE_CHUNK, UPDATE_AGENT_THOUGHT_CHUNK):
        text, is_thinking = parse_text_chunk(update)
        if text:
            events.append(
                AcpEvent(
                    kind=EVENT_THINKING_CHUNK if is_thinking else EVENT_TEXT_CHUNK,
                    text=text,
                    message_id=parse_message_id(update),
                )
            )
        return events
    if kind == UPDATE_TOOL_CALL:
        events.append(
            _build_tool_call_event(
                update,
                tool_input_cache,
                shell_cache,
                raw_params_cache,
                mcp_server_name_cache,
                tool_name_cache,
                cache_scope=cache_scope,
                tool_input_redacted_cache=tool_input_redacted_cache,
                diff_path_cache=diff_path_cache,
                harness_tool_name_cache=harness_tool_name_cache,
                tool_kind_cache=tool_kind_cache,
                cache_overflow=cache_overflow,
                identity_unreadable_cache=identity_unreadable_cache,
            )
        )
        return events
    if kind == UPDATE_TOOL_CALL_UPDATE:
        result = _build_tool_result_event(update, cache_scope)
        if result is not None:
            events.append(result)
        refine = _build_tool_refinement_event(
            update,
            tool_input_cache,
            shell_cache,
            raw_params_cache,
            cache_scope=cache_scope,
            tool_input_redacted_cache=tool_input_redacted_cache,
            diff_path_cache=diff_path_cache,
            cache_overflow=cache_overflow,
            mcp_server_name_cache=mcp_server_name_cache,
            tool_name_cache=tool_name_cache,
            tool_kind_cache=tool_kind_cache,
            identity_unreadable_cache=identity_unreadable_cache,
            harness_tool_name_cache=harness_tool_name_cache,
        )
        if refine is not None:
            events.append(refine)
        # A todo_list result carries the agent's whole task list. Emit it as an
        # ADDITIONAL event rather than swallowing the update — the tool call
        # itself must still render in the transcript like any other. The name
        # cache is what identifies it: this frame carries no _meta of its own.
        todo = parse_todo_snapshot(update, tool_name_cache, cache_scope=cache_scope)
        if todo is not None:
            events.append(
                AcpEvent(
                    kind=EVENT_TODO_UPDATE,
                    tool_call_id=str(update.get("toolCallId") or ""),
                    todo=todo,
                )
            )
        return events
    return events
