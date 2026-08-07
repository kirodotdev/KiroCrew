"""Shared ACP dispatch helpers.

Single source of truth for the request shapes that BOTH ``AcpClient`` (legacy,
process-per-session) and ``AcpRuntime``/``AcpSessionHandle`` (shared runtime,
single-reader demux) must send identically. Keeping these here prevents the
two parallel implementations from drifting.

These are pure, stateless functions: they take primitives and return dicts, so
each class keeps its own I/O model (``_turn_lock`` reader vs per-session queue)
while sharing the data-shaping logic: session/new params, set_mode/set_model
request shapes, per-turn metadata/credit capture, and notification classification.
"""

from __future__ import annotations

import difflib
import json
import logging
import math
import re
from pathlib import Path
from typing import Any

from kiro_crew.acp.types import (
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TODO_UPDATE,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    KIRO_TOOL_TODO_LIST,
    METHOD_AGENT_SWITCHED,
    METHOD_CLEAR_STATUS,
    METHOD_COMPACTION_STATUS,
    METHOD_KIRO_SESSION_UPDATE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    METHOD_METADATA,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_UPDATE,
    METHOD_SET_MODE,
    METHOD_SET_MODEL,
    METHOD_SUBAGENT_LIST_UPDATE,
    OPTION_ALLOW_ALWAYS,
    OPTION_ALLOW_ONCE,
    TODO_TASKS_MAX,
    TODO_TEXT_MAX,
    TOOL_PURPOSE_KEYS,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_TOOL_CALL,
    UPDATE_TOOL_CALL_UPDATE,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)


def build_session_new_params(
    cwd: str | Path,
    *,
    mcp_servers: list[dict[str, Any]] | None = None,
    claude_meta: bool = False,
) -> dict[str, Any]:
    """Build the params for a ``session/new`` request.

    ``cwd`` and ``mcpServers`` are ALWAYS present. kiro-cli treats a missing
    ``mcpServers`` field as malformed and exits cleanly (rc=0, no stderr) — so
    both backends must send it, even as an empty list. The ``claude_meta`` flag
    adds the SDK envelope the claude-agent-acp backend requires.
    """
    params: dict[str, Any] = {
        "cwd": str(cwd),
        "mcpServers": mcp_servers or [],
    }
    if claude_meta:
        params["_meta"] = {"claudeCode": {"options": {}}}
    return params


def set_mode_params(session_id: str, agent: str) -> dict[str, Any]:
    """Params for ``session/set_mode`` (activate an agent on a session)."""
    return {"sessionId": session_id, "modeId": agent}


def parse_session_modes(resp: dict[str, Any]) -> tuple[list[str], str, bool]:
    """Extract advertised mode ids, current mode id, and whether the backend
    advertised a modes list at all, from a ``session/new`` / ``session/load``
    response.

    kiro-cli returns ``modes: {currentModeId, availableModes: [{id, name,
    description}, ...]}`` (parallel to the ``models`` payload). Returns
    ``(ids, current_id, advertised)`` where ``advertised`` is True iff the
    response carried a ``modes`` object with an ``availableModes`` **list**
    (even an empty one).

    The ``advertised`` flag is load-bearing: an OMITTED modes list (older
    kiro-cli / offline fake backend → ``advertised=False``) means "unknown,
    attempt ``set_mode`` for backward compatibility", whereas an ``availableModes:
    []`` that is *present but empty* (``advertised=True``, ``ids=[]``) means the
    backend genuinely offers no modes — the caller must fail closed, not attempt
    a ``set_mode`` that would fault with ``Mode '<agent>' not found``.

    Item id is read from ``id`` first, then ``modeId`` / ``value`` as fallbacks,
    mirroring the defensive shape-reading in ``_normalize_models``. Never raises.
    """
    modes = resp.get("modes")
    if not isinstance(modes, dict):
        return [], "", False
    current = modes.get("currentModeId")
    current_id = current if isinstance(current, str) else ""
    advertised_raw = modes.get("availableModes")
    if not isinstance(advertised_raw, list):
        return [], current_id, False
    ids: list[str] = []
    for m in advertised_raw:
        if not isinstance(m, dict):
            continue
        mode_id = m.get("id") or m.get("modeId") or m.get("value")
        if mode_id:
            ids.append(str(mode_id))
    return ids, current_id, True


def set_model_params(session_id: str, model_id: str) -> dict[str, Any]:
    """Params for ``session/set_model`` (override the model on a session)."""
    return {"sessionId": session_id, "modelId": model_id}


#: Top-level ``_kiro.dev/metadata`` params this parser consumes. ``sessionId`` is
#: consumed a layer up — ``AcpRuntime`` routes every notification by it to the
#: right per-session queue — so reporting it would mislabel a load-bearing routing
#: field as an unhandled discovery on the first frame of every shared-runtime
#: session.
_KNOWN_METADATA_KEYS = frozenset({"contextUsagePercentage", "meteringUsage", "sessionId"})

#: ``meteringUsage`` entry keys this parser knows, and the one ``unit`` value the
#: credit sum reads. An entry with any other unit contributes nothing.
_KNOWN_METERING_KEYS = frozenset({"unit", "unitPlural", "value"})
_KNOWN_METERING_UNITS = frozenset({"credit"})

#: Field names already reported, so a stream of per-turn notifications logs each
#: novel shape once per process rather than on every frame. Two threads racing
#: here can only duplicate a log line, so the hot path takes no lock.
_reported_metadata_fields: set[str] = set()


def _log_unrecognized_metadata_fields(params: dict[str, Any]) -> None:
    """Report ``_kiro.dev/metadata`` fields this parser drops, once each.

    kiro-cli owns the metadata payload, so a field it begins sending — prompt-cache
    counters being the case in point, since ``AcpPromptStats`` already carries
    ``cache_read_tokens``/``cache_creation_tokens`` slots that nothing fills — is
    otherwise discarded with no way to notice.

    What reaches the log is deliberately narrow: field NAMES and value TYPES, plus
    — for ``meteringUsage`` units alone — the unit LABEL itself, because there the
    label IS the signal (``unit=cacheRead`` is the discovery; ``unit:str`` conveys
    nothing, since the unit is always a string). A unit is a low-cardinality
    dimension name drawn from kiro's own billing vocabulary, never a quantity,
    alias, or identifier, so no billing detail reaches the log.
    """
    novel: list[str] = []
    for key, value in params.items():
        if key in _KNOWN_METADATA_KEYS or key in _reported_metadata_fields:
            continue
        _reported_metadata_fields.add(key)
        novel.append(f"{key}:{type(value).__name__}")

    metering = params.get("meteringUsage")
    if isinstance(metering, list):
        for entry in metering:
            if not isinstance(entry, dict):
                continue
            for key, value in entry.items():
                name = f"meteringUsage[].{key}"
                if key in _KNOWN_METERING_KEYS or name in _reported_metadata_fields:
                    continue
                _reported_metadata_fields.add(name)
                novel.append(f"{name}:{type(value).__name__}")
            unit = entry.get("unit")
            # A non-credit unit is silently dropped by the credit sum, so naming it
            # is the only signal that kiro started reporting a new usage dimension.
            if isinstance(unit, str) and unit not in _KNOWN_METERING_UNITS:
                name = f"meteringUsage[].unit={unit}"
                if name not in _reported_metadata_fields:
                    _reported_metadata_fields.add(name)
                    novel.append(name)

    if novel:
        logger.debug("acp metadata: unconsumed field(s) %s", ", ".join(sorted(novel)))


def parse_metadata(params: dict[str, Any]) -> tuple[float | None, float]:
    """Parse a ``_kiro.dev/metadata`` notification's params.

    Returns ``(context_pct_or_None, credits_delta)``. kiro streams per-turn
    billing as ``meteringUsage`` entries with ``unit=="credit"``; token fields
    are 0 for the acp provider, so credits are the real cost signal. Both
    ``AcpClient`` and ``AcpSessionHandle`` call this so the credit-capture
    logic has a single source of truth. The caller applies the values to its own
    ``last_prompt_stats`` (credits are accumulated across the turn).

    Fields outside the two consumed keys are reported once each at debug level by
    :func:`_log_unrecognized_metadata_fields`.
    """
    try:
        _log_unrecognized_metadata_fields(params)
    except Exception:
        # A diagnostic must never break a turn.
        logger.debug("acp metadata: field scan failed", exc_info=True)

    pct = params.get("contextUsagePercentage")
    try:
        pct_val = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        pct_val = None
    credits = 0.0
    metering = params.get("meteringUsage")
    if isinstance(metering, list):
        for entry in metering:
            if isinstance(entry, dict) and entry.get("unit") == "credit":
                try:
                    credits += float(entry.get("value", 0) or 0)
                except (TypeError, ValueError):
                    pass
    return pct_val, credits


def classify_notification(msg: JsonRpcMessage) -> str:
    """Classify an incoming JSON-RPC notification into an action string.

    Single source of truth for the method → action mapping used by the
    shared-runtime dispatch loop (and available to AcpClient when it is later
    reduced to a thin wrapper). Returns ``"skip"`` for frames that carry no
    action, or ``"server_request_unknown"`` for an unrecognized server-initiated
    request that still needs a JSON-RPC reply.
    """
    if msg.is_method(METHOD_REQUEST_PERMISSION):
        return "permission"
    # Mid-turn steer notifications ride the same session-update methods (v2.9
    # `session/update` / v2.7 `_kiro.dev/session/update`); the
    # `update.sessionUpdate` discriminant distinguishes them. Classify as
    # "steer" BEFORE the generic "update" / "subagent_activity" returns so the
    # steer branch in the dispatch loop sees it. Non-steer discriminants fall
    # through unchanged. Shared here so AcpClient and AcpSessionHandle cannot
    # drift on steer recognition.
    if msg.is_method(METHOD_SESSION_UPDATE) or msg.is_method(METHOD_KIRO_SESSION_UPDATE):
        _u = msg.params.get("update") if isinstance(msg.params, dict) else None
        _disc = _u.get("sessionUpdate") if isinstance(_u, dict) else None
        if _disc in (
            "steering_queued",
            "steering_consumed",
            "steering_cleared",
            "AgentExecutionUserMessageQueued",
            "AgentExecutionSteeringInjected",
        ):
            return "steer"
    if msg.is_method(METHOD_SESSION_UPDATE):
        return "update"
    if msg.is_method(METHOD_METADATA):
        return "metadata"
    if msg.is_method(METHOD_COMPACTION_STATUS):
        return "compaction"
    if msg.is_method(METHOD_CLEAR_STATUS):
        return "clear"
    if msg.is_method(METHOD_AGENT_SWITCHED):
        return "agent_switched"
    if msg.is_method(METHOD_MCP_OAUTH_REQUEST):
        return "mcp_oauth_request"
    if msg.is_method(METHOD_MCP_SERVER_INITIALIZED):
        return "mcp_server_initialized"
    if msg.is_method(METHOD_MCP_SERVER_INIT_FAILURE):
        return "mcp_server_init_failure"
    if msg.is_method(METHOD_SUBAGENT_LIST_UPDATE):
        return "subagent_list"
    if msg.is_method(METHOD_KIRO_SESSION_UPDATE):
        return "subagent_activity"
    # Unknown server request — must be answered
    if msg.method is not None and msg.id is not None:
        return "server_request_unknown"
    return "skip"


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
# LLM-influenced fields (titles, inputs, purposes, outputs) happens HERE so tool
# data is never surfaced unredacted. Stats and stale/stall bookkeeping stay
# per-class: the caller walks the returned events.


def make_unified_diff(old: str, new: str, path: str, max_len: int = 6000) -> str:
    """Generate a unified diff string from old/new text, handling empty inputs."""
    old_lines = (old if old.endswith("\n") else old + "\n").splitlines(keepends=True) if old else []
    new_lines = (new if new.endswith("\n") else new + "\n").splitlines(keepends=True) if new else []
    udiff = difflib.unified_diff(old_lines, new_lines, fromfile=path, tofile=path, n=3)
    return "".join(udiff).rstrip()[:max_len]


def select_tool_title(title: object, raw_input: object) -> str | None:
    """Pick the pill label, preferring a human-readable ``description`` when present.

    Some backends' Bash tool emits a ``description`` field alongside ``command``
    (e.g. "List KiroCrew ACP module files" rather than ``ls /workplace/...``).
    We surface it on the pill when supplied; otherwise we fall back to the
    SDK-provided ``title`` (the literal tool invocation). Used for both the
    initial ``tool_call`` and the second-phase ``tool_call_update`` refinement
    so the title rule stays consistent across both events.
    """
    if isinstance(raw_input, dict):
        desc = raw_input.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc
    if isinstance(title, str) and title:
        return title
    return None


def is_tool_purpose_key(key: object) -> bool:
    """True when ``key`` names the reserved tool-purpose argument.

    Matched by SHAPE rather than by an allowlist of literals: any *reserved*
    (dunder-prefixed) argument whose name ends in ``purpose`` once separators
    and case are normalized away. The declared spelling is
    ``__tool_use_purpose``, but the argument reaches us as whatever the model
    actually emitted, and models paraphrase the name — ``__purpose``,
    ``__thinking_purpose`` and ``__woohoo_purpose`` all occur in real
    transcripts. An exact allowlist silently drops every one of them.

    The ``__`` prefix is load-bearing: it keeps a *functional* argument that
    happens to be called ``purpose`` (a tool legitimately taking a purpose
    string) out of the match, because only dunder names are reserved. A tool
    declaring its own dunder ``…purpose`` argument would be read as the purpose
    line, which is the desired reading anyway — and harmless either way, since
    this only picks the label and never rewrites the arguments sent to the tool.
    """
    if not isinstance(key, str) or not key.startswith("__"):
        return False
    return re.sub(r"[^a-z0-9]", "", key.lower()).endswith("purpose")


def extract_tool_purpose(raw_input: object) -> str:
    """Pull the agent-authored purpose line out of a tool call's raw params.

    The canonical spellings in ``TOOL_PURPOSE_KEYS`` are preferred (kiro-cli
    echoes the reserved argument back as either the declared snake_case name or
    a camelCased variant), then any other key matching
    ``is_tool_purpose_key()``. Reading a fixed set of literals drops the purpose
    for every paraphrased spelling, which shows up as the dashboard's concise
    tool pill falling back to the literal command line.

    Off-canonical keys are scanned in sorted order so the choice is
    deterministic when a call somehow carries more than one.
    """
    if not isinstance(raw_input, dict):
        return ""
    for key in TOOL_PURPOSE_KEYS:
        value = raw_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in sorted(k for k in raw_input if is_tool_purpose_key(k)):
        value = raw_input[key]
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _redact(text: str) -> str:
    """Run a string through both redactors (URL exfil + credentials)."""
    if not text:
        return text
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def redact_text(text: str) -> str:
    """Public single-source redaction for LLM-influenced text (sub-agent
    streamed output, tool/sub-agent titles) before it is surfaced on the
    dashboard/Slack. Never trust LLM output — scrub exfil URLs + credentials.
    Both AcpClient and AcpRuntime call this so the two paths stay identical."""
    return _redact(text)


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


_ACP_SHELL_KIND = "execute"


def is_shell_kind(kind: str | None) -> bool:
    """True when an ACP tool_kind denotes a shell/exec command."""
    return kind == _ACP_SHELL_KIND


# Legacy kiro permission options omit the spec-mandated `kind` field. Only
# synthesize a kind for these well-known literals — unknown ids stay empty so we
# don't fabricate intent the agent didn't express. Shared by both transports.
_LEGACY_OPTION_KIND: dict[str, str] = {
    OPTION_ALLOW_ONCE: "allow_once",
    "allow": "allow_once",
    OPTION_ALLOW_ALWAYS: "allow_always",
    "reject_once": "reject_once",
    "reject_always": "reject_always",
}


def build_permission_event(
    msg: JsonRpcMessage,
    *,
    tool_input_cache: dict[str, str] | None = None,
    shell_cache: dict[str, bool] | None = None,
    raw_params_cache: dict[str, dict] | None = None,
    mcp_server_name_cache: dict[str, str] | None = None,
    tool_name_cache: dict[str, str] | None = None,
) -> tuple[AcpEvent, dict[str, str] | None]:
    """Build an ``EVENT_PERMISSION_REQUEST`` from a ``session/request_permission``.

    Single source of truth shared by ``AcpClient`` and ``AcpSessionHandle`` so
    the two transports cannot drift on the kiro/claude permission payload shape:
    kiro nests the tool info under ``params["toolCall"]`` (not a flat
    ``params["title"]``), so reading the flat field leaves ``title`` /
    ``is_shell`` empty and trips the host trust-mode gate.

    Returns ``(event, recorded_options)`` where ``recorded_options`` is the
    ``{"once","always","reject"}`` optionId map the caller stores on the request
    id so ``approve_tool`` / ``reject_tool`` can echo the exact ids the agent
    advertised (``None`` when no allow/reject option was advertised).

    ``tool_input_cache`` (caller-owned ``toolCallId -> redacted input``) is
    consulted to recover the full tool input the preceding ``tool_call``
    notification carried; ``shell_cache`` (caller-owned ``toolCallId -> is_shell``)
    is the ONLY trusted source for the shell signal (deny-by-default — the
    permission payload's own ``kind`` is agent-influenced and must not waive the
    tool-name length cap).
    """
    request_id = msg.id if msg.id is not None else ""
    params = msg.params or {}
    tool_call = params.get("toolCall", {})
    tool_call = tool_call if isinstance(tool_call, dict) else {}
    title = _redact(tool_call.get("title", "unknown"))
    # The ACP toolCall carries a `kind` ("execute" for Bash, "read"/"edit"/…).
    # Carry it onto the event as display/telemetry metadata only — the is_shell
    # length-cap exemption resolves from shell_cache below, never this field.
    tool_kind = tool_call.get("kind", "")

    # ACP spec uses optionId/name + kind ("allow_once"|"allow_always"|
    # "reject_once"|"reject_always"); kiro-cli historically uses id/label with id
    # values "allow_once"/"allow_always". Accept both shapes and remember the
    # actual optionIds keyed by kind so approve/reject can echo the exact id.
    options: list[dict[str, str]] = []
    kind_to_id: dict[str, str] = {}
    raw_options = params.get("options", [])
    for o in raw_options if isinstance(raw_options, list) else []:
        if not isinstance(o, dict):
            continue
        opt_id = o.get("optionId") or o.get("id") or ""
        opt_label = o.get("name") or o.get("label") or ""
        opt_kind = o.get("kind") or ""
        # A truthy non-string id would crash opt_id.lower() below (and
        # non-string label/kind would leak into the typed options list).
        if not isinstance(opt_id, str) or not opt_id:
            continue
        if not isinstance(opt_label, str):
            opt_label = ""
        if not isinstance(opt_kind, str):
            opt_kind = ""
        options.append({"id": opt_id, "label": opt_label})
        if not opt_kind:
            opt_kind = _LEGACY_OPTION_KIND.get(opt_id.lower(), "")
        if opt_kind:
            kind_to_id.setdefault(opt_kind, opt_id)
    if not options:
        options = [
            {"id": OPTION_ALLOW_ONCE, "label": "Allow once"},
            {"id": OPTION_ALLOW_ALWAYS, "label": "Allow always"},
        ]
        kind_to_id = {"allow_once": OPTION_ALLOW_ONCE, "allow_always": OPTION_ALLOW_ALWAYS}

    # Record optionIds the agent advertised so approve_tool / reject_tool can
    # echo the exact ids. Record when EITHER an allow option (for approve) OR a
    # reject option (for a clean reject) was advertised. claude-agent-acp offers
    # a {kind:"reject_once", optionId:"reject"} whose selection yields
    # behavior:"deny" — far better than a "cancelled" outcome, which the adapter
    # turns into a cryptic "Tool use aborted". kiro-cli advertises no reject
    # option, so reject_tool falls back to "cancelled" (a clean rejection there).
    any_allow = kind_to_id.get("allow_once") or kind_to_id.get("allow_always")
    any_reject = kind_to_id.get("reject_once") or kind_to_id.get("reject_always")
    recorded: dict[str, str] | None = None
    if request_id != "" and (any_allow is not None or any_reject is not None):
        recorded = {}
        if any_allow is not None:
            recorded["once"] = kind_to_id.get("allow_once") or any_allow
            recorded["always"] = kind_to_id.get("allow_always") or any_allow
        if any_reject is not None:
            recorded["reject"] = any_reject

    # Resolve full tool input — the preceding tool_call notification carries the
    # complete params cached by toolCallId; the permission message only has a
    # truncated human-readable title.
    tool_call_id = tool_call.get("toolCallId", "")
    tool_input = ""
    if tool_call_id and tool_input_cache is not None and tool_call_id in tool_input_cache:
        tool_input = tool_input_cache.pop(tool_call_id)
    if not tool_input:
        raw_input = tool_call.get("input") or tool_call.get("params")
        if raw_input:
            tool_input = (
                json.dumps(raw_input, indent=2)
                if isinstance(raw_input, (dict, list))
                else str(raw_input)
            )
            # SECURITY: the primary path (tool_input_cache) is already redacted
            # by the tool_call parser; on a cache miss this fallback reads raw
            # LLM-influenced input that surfaces on the dashboard permission UI,
            # so scrub exfil URLs + credentials before it leaves this function.
            tool_input = redact_text(tool_input)

    # Resolve the canonical shell signal. SECURITY (deny-by-default): the ONLY
    # trusted source is the value cached from the preceding tool_call (keyed by
    # toolCallId). We deliberately do NOT fall back to the permission payload's
    # own `kind` — that field is agent/LLM-influenced, and trusting it to waive
    # the tool-name length cap on the very name being validated would let a
    # malicious agent set kind="execute" to bypass the check. On a cache miss
    # is_shell stays False and the length cap is enforced. Use .get() (not
    # .pop()): a later tool_call_update refinement reads this same cache, so
    # popping here would make it wrongly report is_shell=False.
    cached_shell = (
        shell_cache.get(tool_call_id) if (shell_cache is not None and tool_call_id) else None
    )
    is_shell = bool(cached_shell)
    if cached_shell is None and tool_input:
        logger.info(
            "Permission event resolved tool_input but missed is_shell cache "
            "(req=%s tool_call_id=%s)",
            request_id,
            tool_call_id,
        )

    # Resolve the STRUCTURED raw params for governance enforcement. The keystone
    # sensitive-path + write-protected-config checks (hooks.on_tool_call) read
    # event.raw_tool_params (a dict) — NOT the display title — so it must be set
    # or a title that hides the path (e.g. a generic "Editing" title over an SSH
    # key / security_policy.json) would slip past the arg-derived gate on this
    # shared path. Primary source: the raw dict the preceding tool_call cached by
    # toolCallId; fallback: an inline dict on the permission frame itself.
    _resolved_raw_params: dict | None = None
    if tool_call_id and raw_params_cache is not None:
        _resolved_raw_params = raw_params_cache.pop(tool_call_id, None)
    if _resolved_raw_params is None:
        _inline = tool_call.get("input") or tool_call.get("params")
        if isinstance(_inline, dict):
            _resolved_raw_params = _inline

    event = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        title=title,
        tool_kind=tool_kind,
        options=options,
        tool_input=tool_input,
        tool_call_id=tool_call_id,
        raw_tool_params=_resolved_raw_params,
        is_shell=is_shell,
        # Trusted MCP server identity recovered from the preceding tool_call
        # (the permission payload carries no _meta). .get() (not .pop()) mirrors
        # the is_shell cache: a later tool_call_update for the same id re-reads
        # it; the per-turn dispatch .clear() handles cleanup. Empty on a miss
        # (fail-closed for the app-own-server auto-approve).
        mcp_server_name=(
            mcp_server_name_cache.get(tool_call_id, "")
            if (mcp_server_name_cache is not None and tool_call_id)
            else ""
        ),
        # Trusted tool identity recovered from the preceding tool_call, mirroring
        # mcp_server_name above. Lets the app-own-server auto-approve govern the
        # canonical mcp__<server>__<tool> on the permission path (no _meta here).
        # Empty on a miss (fail-closed: no trusted tool name → no auto-approve).
        tool_name=(
            tool_name_cache.get(tool_call_id, "")
            if (tool_name_cache is not None and tool_call_id)
            else ""
        ),
    )
    return event, recorded


def _build_tool_call_event(
    update: dict[str, Any],
    tool_input_cache: dict[str, str] | None,
    shell_cache: dict[str, bool] | None = None,
    raw_params_cache: dict[str, dict] | None = None,
    mcp_server_name_cache: dict[str, str] | None = None,
    tool_name_cache: dict[str, str] | None = None,
) -> AcpEvent:
    """Build an ``EVENT_TOOL_CALL`` from a ``tool_call`` update (with redaction)."""
    title = update.get("title", "unknown")
    kind = update.get("kind", "unknown")
    raw_input = update.get("rawInput") or update.get("input") or update.get("params")
    purpose = extract_tool_purpose(raw_input)
    tool_call_id = update.get("toolCallId", "")
    # Cache the STRUCTURED raw params (dict) keyed by toolCallId so a later
    # permission_request — which carries only a truncated title — can recover
    # them for governance enforcement (raw_tool_params). Mirrors AcpClient's
    # _tool_call_params. shell_cache/tool_input_cache below serve display/is_shell.
    if tool_call_id and raw_params_cache is not None and isinstance(raw_input, dict):
        raw_params_cache[tool_call_id] = raw_input
    # Capture the shell signal from the RAW kind (before redaction) so a later
    # permission_request (which carries no kind) can inherit it via shell_cache.
    is_shell = is_shell_kind(kind)
    if tool_call_id and shell_cache is not None:
        shell_cache[tool_call_id] = is_shell
    # Capture the TRUSTED MCP server identity (_meta.kiro.mcpServerName) so the
    # later permission_request — the dashboard's gate path, which carries no
    # _meta — can inherit it via mcp_server_name_cache. This is what lets the
    # app-own-server auto-approve (hooks.on_tool_call) fire on the permission
    # path: without the cache, the permission event's mcp_server_name is always
    # "" and the branch never matches.
    _mcp_server_name = _kiro_mcp_server_name(update)
    if tool_call_id and mcp_server_name_cache is not None:
        mcp_server_name_cache[tool_call_id] = _mcp_server_name
    # Same lifecycle for the trusted tool name (_meta.kiro.toolName) so the
    # permission event can reconstruct the canonical mcp__<server>__<tool> for
    # per-tool governance in the app-own-server auto-approve.
    _tool_name = _kiro_tool_name(update)
    if tool_call_id and tool_name_cache is not None:
        tool_name_cache[tool_call_id] = _tool_name
    # Initial tool input string from raw params.
    input_str = ""
    if tool_call_id and raw_input:
        input_str = (
            json.dumps(raw_input, indent=2)
            if isinstance(raw_input, (dict, list))
            else str(raw_input)
        )
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
                _diff_path = cb.get("path") or ""
                diff_str = make_unified_diff(
                    _diff_old_text or "", cb.get("newText") or "", _diff_path
                )
                if diff_str:
                    input_str = diff_str
                    found_diff = True
                break
    # Fallback for strReplace when no diff content block was present.
    if not found_diff and isinstance(raw_input, dict) and raw_input.get("command") == "strReplace":
        old = raw_input.get("oldStr") or ""
        new = raw_input.get("newStr") or ""
        if old or new:
            diff_str = make_unified_diff(old, new, raw_input.get("path") or "")
            if diff_str:
                input_str = diff_str
    if input_str:
        input_str = _redact(input_str)
    if tool_call_id and input_str and tool_input_cache is not None:
        tool_input_cache[tool_call_id] = input_str
    if purpose:
        purpose = _redact(purpose)
    title = select_tool_title(title, raw_input) or ""
    if title:
        title = _redact(title)
    if kind:
        kind = _redact(kind)
    return AcpEvent(
        kind=EVENT_TOOL_CALL,
        title=title,
        tool_kind=kind,
        tool_purpose=purpose,
        tool_input=input_str,
        tool_call_id=tool_call_id,
        raw_tool_params=raw_input if isinstance(raw_input, dict) else None,
        is_shell=is_shell,
        # Trusted identity from _meta.kiro (NOT the LLM-authored title).
        tool_name=_tool_name,
        mcp_server_name=_mcp_server_name,
        diff_old_text=_diff_old_text,
        diff_path=_diff_path,
    )


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


def _build_tool_result_event(update: dict[str, Any]) -> AcpEvent | None:
    """Build an ``EVENT_TOOL_RESULT`` from a ``tool_call_update`` carrying output.

    Two output shapes: ``content[].content.text`` blocks (stream mid-turn), or
    ``rawOutput.items[]`` (``Text`` / ``Json.stdout``) on ``status=completed``.
    Returns None when the update carries no output (refinement-only updates are
    handled by :func:`_build_tool_refinement_event`).
    """
    tool_use_id = update.get("toolCallId", "")
    if not tool_use_id:
        return None
    output_parts: list[str] = []
    # Path 1: content blocks (mid-stream).
    content = update.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            inner = block.get("content")
            if isinstance(inner, dict) and inner.get("type") == "text":
                text = inner.get("text", "")
                if text:
                    output_parts.append(str(text)[:4000])
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
                        output_parts.append(str(item["Text"])[:4000])
                        continue
                    j = item.get("Json")
                    if isinstance(j, dict):
                        if "stdout" in j and j.get("stdout"):
                            output_parts.append(str(j["stdout"])[:4000])
                        else:
                            _mcp_text = _mcp_content_text(j)
                            if _mcp_text is not None:
                                output_parts.append(_mcp_text[:4000])
                            else:
                                output_parts.append(json.dumps(j, default=str)[:4000])
    if not output_parts:
        return None
    final_output = _redact("\n".join(output_parts)[:8000])
    return AcpEvent(
        kind=EVENT_TOOL_RESULT,
        tool_call_id=tool_use_id,
        tool_output=final_output,
        tool_final=update.get("status") == "completed",
    )


def _kiro_tool_name(update: dict[str, Any]) -> str:
    """The real tool name from ``_meta.kiro.toolName``, or "" when absent.

    The user-visible ``title`` is LLM-authored prose ("Creating task list: …"),
    so it cannot be used to identify a tool. Only this ``_meta`` channel is
    stable.
    """
    meta = update.get("_meta")
    if not isinstance(meta, dict):
        return ""
    kiro = meta.get("kiro")
    if not isinstance(kiro, dict):
        return ""
    name = kiro.get("toolName")
    return name if isinstance(name, str) else ""


def _kiro_mcp_server_name(update: dict[str, Any]) -> str:
    """The MCP server name from ``_meta.kiro.mcpServerName``, or "" for
    built-in/shell tools.

    kiro-cli sets this ONLY for MCP-served tool calls (see
    ``kiro_tool_identity_meta`` in the engine), so a non-empty value is the
    trusted discriminator "this tool call was served by an MCP server" — the
    signal a security gate needs to tell a genuine MCP directive tool from a
    shell command whose stdout the model authored.
    """
    meta = update.get("_meta")
    if not isinstance(meta, dict):
        return ""
    kiro = meta.get("kiro")
    if not isinstance(kiro, dict):
        return ""
    name = kiro.get("mcpServerName")
    return name if isinstance(name, str) else ""


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


def parse_todo_snapshot(update: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise a ``todo_list`` tool result into a UI-ready snapshot.

    Returns ``{description, tasks: [{id, text, completed}]}`` or None when this
    update is not a todo_list result. EVERY todo_list command (create / complete
    / list) echoes the entire list, so the return value is always a full
    snapshot — callers replace their stored copy rather than merging.

    An empty ``tasks`` list is a MEANINGFUL result (the agent cleared its list),
    so it returns a snapshot with zero tasks rather than None. Only a genuine
    non-match or unparseable payload yields None.
    """
    if not isinstance(update, dict):
        return None
    if _kiro_tool_name(update) != KIRO_TOOL_TODO_LIST:
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
                "id": str(task_id) if task_id is not None else str(idx + 1),
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


def _build_tool_refinement_event(
    update: dict[str, Any],
    tool_input_cache: dict[str, str] | None,
    shell_cache: dict[str, bool] | None = None,
) -> AcpEvent | None:
    """Build an ``EVENT_TOOL_CALL_UPDATE`` (refined title/kind/input) for a tool.

    claude-agent-acp emits a follow-up ``tool_call_update`` once the streamed
    ``rawInput`` is complete (the initial ``tool_call`` had empty input + a
    generic title). Returns None when the update carries no refinement fields
    (pure-output updates are handled by :func:`_build_tool_result_event`).
    """
    tool_use_id = update.get("toolCallId", "")
    if not tool_use_id:
        return None
    title = update.get("title")
    kind = update.get("kind")
    raw_input = update.get("rawInput")
    if title is None and kind is None and not raw_input:
        return None
    input_str = ""
    if isinstance(raw_input, (dict, list)) and raw_input:
        try:
            input_str = json.dumps(raw_input, indent=2)
        except (TypeError, ValueError):
            input_str = str(raw_input)
    elif isinstance(raw_input, str):
        input_str = raw_input
    content_blocks = update.get("content", [])
    _diff_old_text: str | None = None
    _diff_path: str = ""
    if isinstance(content_blocks, list):
        for cb in content_blocks:
            if isinstance(cb, dict) and cb.get("type") == "diff":
                _cb_old = cb.get("oldText")
                _diff_old_text = _cb_old if isinstance(_cb_old, str) else (_cb_old or "")
                _diff_path = cb.get("path") or ""
                diff_str = make_unified_diff(
                    _diff_old_text or "", cb.get("newText") or "", _diff_path
                )
                if diff_str:
                    input_str = diff_str
                break
    if input_str:
        input_str = _redact(input_str)
        if tool_input_cache is not None:
            tool_input_cache[tool_use_id] = input_str
    title_source = select_tool_title(title, raw_input)
    title_str = _redact(title_source) if title_source else ""
    kind_str = _redact(kind) if isinstance(kind, str) and kind else ""
    # Refresh the cached shell signal only when this refinement carries a kind
    # (kind is optional on updates); a kind-less refinement must not clobber a
    # True cached by the initial tool_call. Mirrors AcpClient exactly.
    if shell_cache is not None:
        if isinstance(kind, str) and kind:
            shell_cache[tool_use_id] = is_shell_kind(kind)
        is_shell = shell_cache.get(tool_use_id, False)
    else:
        is_shell = is_shell_kind(kind) if isinstance(kind, str) and kind else False
    return AcpEvent(
        kind=EVENT_TOOL_CALL_UPDATE,
        title=title_str,
        tool_kind=kind_str,
        tool_input=input_str,
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
) -> list[AcpEvent]:
    """Parse one ``session/update`` inner ``update`` dict into ``AcpEvent``s.

    Single source of truth shared by ``AcpClient`` and ``AcpRuntime``. Returns a
    list (0–2 events) so a ``tool_call_update`` can yield BOTH a result and a
    refinement in the same order the legacy client emitted them (result first).
    ``usage_update`` is NOT an event — use :func:`parse_usage_update`.

    ``tool_input_cache`` (caller-owned) is written with ``toolCallId -> redacted
    input`` for ``tool_call`` / refinement updates, mirroring each class's
    ``_tool_call_inputs`` map. Stats and stall bookkeeping stay with the caller.
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
            )
        )
        return events
    if kind == UPDATE_TOOL_CALL_UPDATE:
        result = _build_tool_result_event(update)
        if result is not None:
            events.append(result)
        refine = _build_tool_refinement_event(update, tool_input_cache, shell_cache)
        if refine is not None:
            events.append(refine)
        # A todo_list result carries the agent's whole task list. Emit it as an
        # ADDITIONAL event rather than swallowing the update — the tool call
        # itself must still render in the transcript like any other.
        todo = parse_todo_snapshot(update)
        if todo is not None:
            events.append(AcpEvent(kind=EVENT_TODO_UPDATE, todo=todo))
        return events
    return events


def _token_count(value: Any) -> int | float | None:
    """Validate an agent-supplied token count; None for anything unusable.

    The value comes straight from the agent process. Non-numbers (str/list/
    bool) would crash comparisons or division downstream; json parses NaN/
    Infinity literals to non-finite floats that pass isinstance but crash
    int(); and an arbitrary-precision int beyond float range makes
    math.isfinite itself raise OverflowError. All of those run inside the
    prompt-turn dispatch path, so a malformed value must degrade to "absent",
    never raise.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    return value


def parse_usage_update(update: dict[str, Any]) -> tuple[int | float | None, int | float | None]:
    """Parse a ``usage_update`` into validated ``(used, size)`` token counts.

    kiro-cli emits a FLAT shape (``update.used`` / ``update.size``); this reads
    flat-primary with a nested ``update.usage.*`` fallback so both classes read
    identically regardless of which shape kiro emits.

    Values are validated via ``_token_count`` so BOTH consumers
    (``AcpClient._track_usage_update`` and ``AcpSessionHandle._handle_update``)
    are safe against malformed payloads at one chokepoint.
    """
    if not isinstance(update, dict):
        return None, None
    used = update.get("used")
    size = update.get("size")
    if used is None or size is None:
        nested = update.get("usage")
        if isinstance(nested, dict):
            if used is None:
                used = nested.get("used")
            if size is None:
                size = nested.get("size")
    return _token_count(used), _token_count(size)


# Re-export the method names so callers can use a single import site for the
# kiro handshake (mode/model) requests alongside the param builders.
__all__ = [
    "build_session_new_params",
    "set_mode_params",
    "set_model_params",
    "parse_metadata",
    "classify_notification",
    "build_permission_event",
    "parse_session_update",
    "parse_usage_update",
    "parse_text_chunk",
    "make_unified_diff",
    "select_tool_title",
    "is_shell_kind",
    "redact_text",
    "METHOD_SET_MODE",
    "METHOD_SET_MODEL",
]
