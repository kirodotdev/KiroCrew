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

import importlib as _importlib
import logging
import math
import sys as _sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew import _facade_mirror, mcp_apps_render, session_directive  # noqa: F401
from kiro_crew.acp.elicitation import is_elicitation_request
from kiro_crew.acp.harness_tool_names import (  # noqa: F401
    CLAUDE_READ_ONLY_BUILTINS,
    GOOSE_READ_ONLY_BUILTINS,
    MAX_HARNESS_TOOL_NAME_LEN,
    qualified_harness_tool_id,
)
from kiro_crew.acp.types import (  # noqa: F401
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
    STOP_REASON_CONTENT_FILTERED_WIRE,
    TERMINAL_TOOL_STATUSES,
    TODO_ID_MAX,
    TODO_TASKS_MAX,
    TODO_TEXT_MAX,
    TOOL_PURPOSE_KEYS,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_TOOL_CALL,
    UPDATE_TOOL_CALL_UPDATE,
    AcpEvent,
    JsonRpcMessage,
    RefusalInfo,
)
from kiro_crew.acp_backends import (  # noqa: F401
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_META_IDENTITY,
)
from kiro_crew.agent_files import kas_wire_agent_id
from kiro_crew.mcp_gateway.tool_surface import (  # noqa: F401
    mcp_identity_name,
    mcp_identity_unreadable,
)
from kiro_crew.metrics.tool_calls import (  # noqa: F401
    note_tool_call_started,
    record_tool_call_finished,
)
from kiro_crew.platform.guest_paths import GuestPathParams  # noqa: F401
from kiro_crew.platform.tool_paths import WRITE_PLANE_KINDS  # noqa: F401
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.security.credential_sources import tool_output_fingerprints  # noqa: F401
from kiro_crew.session_environment import (  # noqa: F401
    WORKSPACE_DENY_ALIASES,
    WORKSPACE_TOOL_KINDS,
)

logger = logging.getLogger(__name__)

# Starvation guard for both read loops (AcpClient._prompt_loop and
# AcpSessionHandle._dispatch_events): the longest one task step may hold the
# event loop before it must yield. Neither loop's read is a suspension point
# when input is already buffered -- ``asyncio.wait_for`` runs its awaitable
# inline, ``Queue.get`` returns without awaiting on a non-empty queue, and
# ``StreamReader.readline`` returns without awaiting when the line is already
# in its buffer -- so a backlog drains entirely inside one task step, starving
# every other session, the dashboard websocket and the loop-stall heartbeat for
# the whole drain. Budgeted by wall clock rather than frame count because
# per-frame consumer cost varies by orders of magnitude, and loop-held time is
# the quantity the watchdog measures. Shared so the two loops cannot drift.
DRAIN_YIELD_AFTER_S = 0.05


def build_session_new_params(
    cwd: str | Path,
    *,
    mcp_servers: list[dict[str, Any]] | None = None,
    claude_meta: bool = False,
    kas_custom_agents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the params for a ``session/new`` request.

    ``cwd`` and ``mcpServers`` are ALWAYS present. kiro-cli treats a missing
    ``mcpServers`` field as malformed and exits cleanly (rc=0, no stderr) — so
    both backends must send it, even as an empty list. The ``claude_meta`` flag
    adds the SDK envelope the claude-agent-acp backend requires.

    ``kas_custom_agents`` carries agent definitions to KAS, which has no
    ``--agent`` flag and advertises only its own built-in modes: an injected
    agent is registered, surfaces as a mode, and can then be activated by the
    ordinary ``session/set_mode``. The two ``_meta`` envelopes belong to
    different backends, so they never both apply.
    """
    params: dict[str, Any] = {
        "cwd": str(cwd),
        "mcpServers": mcp_servers or [],
    }
    if claude_meta:
        params["_meta"] = {"claudeCode": {"options": {}}}
    if kas_custom_agents:
        attach_kas_custom_agents(params, kas_custom_agents)
    return params


def attach_kas_custom_agents(
    params: dict[str, Any],
    agents: list[dict[str, Any]] | None,
) -> None:
    """Put agent definitions in the ``_meta`` envelope KAS reads them from.

    Shared by ``session/new`` and ``session/load`` so the envelope's shape has
    one owner: a resumed session needs the same definitions a new one gets, and
    two hand-built copies of the same nesting would be free to drift.

    Merges into any existing ``_meta`` rather than replacing it. Today's other
    writer is a kiro-cli-only transcript path, so in practice they never both
    apply — but a future third writer should not be able to silently drop one.

    Each entry goes out under :func:`~kiro_crew.agent_files.kas_wire_agent_id`,
    so a crewmate whose id the engine keeps for itself still registers. The
    caller's list is not mutated: everything else reads it by the crewmate's id.
    """
    if not agents:
        return
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
        params["_meta"] = meta
    wire = [
        (
            {**a, "id": kas_wire_agent_id(a["id"])}
            if isinstance(a, dict) and isinstance(a.get("id"), str)
            else a
        )
        for a in agents
    ]
    meta["kiro"] = {"customAgents": wire}


def kas_crew_agent_id(agents: Any, mode_id: str) -> str:
    """The crewmate id behind *mode_id*, a mode id the KAS engine reported.

    The inverse of the rename :func:`attach_kas_custom_agents` applies, read
    from the batch the session registered, so it can only ever map back to an
    agent Kiro Crew sent. Any other id (a KAS built-in) is returned unchanged.
    """
    for a in agents if isinstance(agents, list) else ():
        agent_id = a.get("id") if isinstance(a, dict) else None
        if isinstance(agent_id, str) and kas_wire_agent_id(agent_id) == mode_id:
            return agent_id
    return mode_id


def set_mode_params(session_id: str, agent: str) -> dict[str, Any]:
    """Params for ``session/set_mode`` (activate an agent on a session)."""
    return {"sessionId": session_id, "modeId": agent}


def agent_version_from_init(resp: dict[str, Any]) -> str:
    """``agentInfo.version`` from an ``initialize`` result, ``""`` when absent.

    Shared by both clients so the two handshakes read the same field the same
    way: kiro-cli reports ``{"agentInfo": {"name": ..., "version": "2.21.0"}}``.
    A missing or non-string value reads as unknown rather than raising — the
    handshake must not fail over a field only a capability gate consults.
    """
    info = resp.get("agentInfo") if isinstance(resp, dict) else None
    if not isinstance(info, dict):
        return ""
    version = info.get("version")
    return version.strip() if isinstance(version, str) else ""


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


def advertised_mode_origin(resp: dict[str, Any], mode_id: str) -> str:
    """Who supplied the definition behind an advertised mode, or ``""``.

    kiro-cli's v3 engine stamps each ``availableModes`` entry with
    ``_meta.kiro.resource.source.origin``: ``"client"`` for a definition that
    arrived as a ``_meta.kiro.customAgents`` entry on this session, ``"bundled"``
    for one of its own built-in agents (and for an agent it read from disk).
    That stamp is the only place the wire says whether an injected id was
    REGISTERED or silently shadowed: when a client entry collides with a
    built-in id the engine keeps its own definition and advertises the mode
    under that id anyway, so ``id in availableModes`` alone reads as success.
    Returns ``""`` when the mode is absent or carries no such stamp (an older
    engine, kiro-cli, the offline fake), so a caller can only ever fail closed
    on a POSITIVE non-client stamp. Never raises.
    """
    modes = resp.get("modes")
    if not isinstance(modes, dict):
        return ""
    advertised_raw = modes.get("availableModes")
    if not isinstance(advertised_raw, list):
        return ""
    for m in advertised_raw:
        if not isinstance(m, dict):
            continue
        if str(m.get("id") or m.get("modeId") or m.get("value") or "") != mode_id:
            continue
        meta = m.get("_meta")
        kiro = meta.get("kiro") if isinstance(meta, dict) else None
        resource = kiro.get("resource") if isinstance(kiro, dict) else None
        source = resource.get("source") if isinstance(resource, dict) else None
        origin = source.get("origin") if isinstance(source, dict) else None
        return origin if isinstance(origin, str) else ""
    return ""


def set_model_params(session_id: str, model_id: str) -> dict[str, Any]:
    """Params for ``session/set_model`` (override the model on a session)."""
    return {"sessionId": session_id, "modelId": model_id}


#: Top-level ``_kiro.dev/metadata`` params this parser consumes. ``sessionId`` is
#: consumed a layer up — ``AcpRuntime`` routes every notification by it to the
#: right per-session queue — so reporting it would mislabel a load-bearing routing
#: field as an unhandled discovery on the first frame of every shared-runtime
#: session.
#:
#: ``stopReason`` and ``refusal`` are the content-filter envelope read by
#: :func:`parse_refusal` (``ACP_BACKENDS_STRUCTURED_REFUSAL``).
_KNOWN_METADATA_KEYS = frozenset(
    {"contextUsagePercentage", "meteringUsage", "sessionId", "stopReason", "refusal"}
)

#: Keys of the ``refusal`` object :func:`parse_refusal` consumes. Anything else
#: the service adds is reported once like any other unconsumed field.
_KNOWN_REFUSAL_KEYS = frozenset({"category", "explanation", "recommendedModel"})

#: Longest ``explanation`` carried onto the dashboard. The canned text is ~250
#: chars; the cap is a guard against a provider echoing the prompt back.
_REFUSAL_EXPLANATION_MAX = 600

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

    refusal = params.get("refusal")
    if isinstance(refusal, dict):
        for key, value in refusal.items():
            name = f"refusal.{key}:{type(value).__name__}"
            if key in _KNOWN_REFUSAL_KEYS or name in _reported_metadata_fields:
                continue
            _reported_metadata_fields.add(name)
            novel.append(name)

    if novel:
        logger.debug("acp metadata: unconsumed field(s) %s", ", ".join(sorted(novel)))


def _refusal_str(value: object, limit: int) -> str:
    """A provider string bound for the dashboard: str-typed, scrubbed, capped.

    EVERY field of the refusal object goes through this, not just the prose
    one: ``category`` and ``recommendedModel`` are provider-echoed text
    reaching the same log line and the same card as ``explanation``, and the
    service's vocabulary being closed is an upstream assumption this side
    cannot verify. Redaction runs over the FULL value, and only then the cap:
    any cut before the redactors -- the cap itself, or a "generous" pre-slice --
    can split a secret so its prefix no longer matches a pattern and reaches
    the surface raw. A refusal object is a few hundred bytes, so scanning it
    whole costs nothing.
    """
    if not isinstance(value, str):
        return ""
    text, _ = redact_exfiltration_urls(value.strip())
    text, _ = redact_credentials(text)
    return text[:limit]


#: The one JSON-RPC error the Kiro service is observed to send as a content-filter
#: refusal's terminal: a bare ``-32603 Internal error`` with no ``data``.
_REFUSAL_TERMINAL_CODE = -32603


def error_is_refusal_terminal(error: object, refusal: RefusalInfo | None) -> bool:
    """True iff *error* is the terminal frame OF a refusal already recorded.

    Two conditions, both required. A refusal must have arrived on metadata this
    turn -- with none recorded, every error is an ordinary error. And the frame
    must be the bare ``-32603`` the service sends for that case: code alone,
    ``data`` empty or absent. Anything else -- a prompt-busy echo, a model
    rejection, a throttle, any frame carrying provider ``data`` -- keeps its own
    classification even when it happens to land after a refusal frame, so the
    retry ladder, the substitute-model path and the prompt-busy reset all still
    see the failure they exist for. Scoping here rather than at each of the four
    prompt-terminal readers is what keeps them from drifting apart.

    Logs at WARNING when it answers True: the frame is about to be consumed
    without reaching ``_raise_acp_error``, and that must be visible in the log
    even though it is the intended outcome.
    """
    if refusal is None or not isinstance(error, dict):
        return False
    try:
        code = int(error.get("code"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if code != _REFUSAL_TERMINAL_CODE:
        return False
    data = error.get("data")
    if data not in (None, "", {}, []):
        return False
    logger.warning(
        "acp: -32603 error frame consumed as the terminal of a content-filter "
        "refusal (category=%s); not raised, not retried",
        refusal.category or "-",
    )
    return True


def parse_refusal(params: dict[str, Any]) -> RefusalInfo | None:
    """Read a content-filter refusal off a ``_kiro.dev/metadata`` notification.

    The Kiro service reports a declined turn as ``stopReason: CONTENT_FILTERED``
    with a ``refusal`` object beside it, on the metadata channel rather than on
    the terminal. Returns a :class:`RefusalInfo` when EITHER signal is present
    -- the stop reason alone (an object-less refusal still is one), or a
    ``refusal`` object alone (a future stop-reason spelling must not hide the
    reason the service did send) -- and ``None`` for the ordinary per-turn
    usage frame that carries neither.

    ``explanation`` is provider text that reaches the dashboard, so it passes
    the same two-pass scrub every other backend-echoed string does. Field values
    are never logged here: the caller logs only that a refusal was seen.
    """
    stop_reason = params.get("stopReason")
    refusal = params.get("refusal")
    filtered = isinstance(stop_reason, str) and (
        stop_reason.strip().upper() == STOP_REASON_CONTENT_FILTERED_WIRE
    )
    if not filtered and not isinstance(refusal, dict):
        return None
    if not isinstance(refusal, dict):
        refusal = {}
    info = RefusalInfo(
        category=_refusal_str(refusal.get("category"), 64),
        explanation=_refusal_str(refusal.get("explanation"), _REFUSAL_EXPLANATION_MAX),
        recommended_model=_refusal_str(refusal.get("recommendedModel"), 128),
    )
    # A log line naming the (scrubbed) category is how an operator learns WHICH
    # filter a fleet keeps tripping, without carrying the explanation (prose)
    # or the prompt.
    logger.info(
        "acp metadata: content-filter refusal (category=%s, recommended_model=%s)",
        info.category or "-",
        info.recommended_model or "-",
    )
    return info


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
    except (TypeError, ValueError, OverflowError):
        # OverflowError: a JSON integer beyond float range — malformed telemetry
        # must degrade to "absent", never raise inside the turn dispatch path.
        pct_val = None
    credits = 0.0
    metering = params.get("meteringUsage")
    if isinstance(metering, list):
        for entry in metering:
            if isinstance(entry, dict) and entry.get("unit") == "credit":
                try:
                    credits += float(entry.get("value", 0) or 0)
                except (TypeError, ValueError, OverflowError):
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
    # An MCP server's elicitation, on either wire (standard `elicitation/create`
    # or KAS's `_kiro/mcp/elicitation`). Only a REQUEST (with an id) is one:
    # a notification on the prefix (the standard `elicitation/complete`) falls
    # through to "skip" like every other notification nothing reads.
    if is_elicitation_request(msg):
        return "elicitation"
    # Unknown server request — must be answered
    if msg.method is not None and msg.id is not None:
        return "server_request_unknown"
    return "skip"


if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.acp._dispatch_parts.adapter_signals import (  # noqa: F401
        _CLAUDE_COMPACTION_COMPLETED_MARKER,
        _CLAUDE_COMPACTION_FAILED_MARKER,
        _CLAUDE_COMPACTION_STARTED_MARKER,
        _CODEX_ADAPTER_NOTICE_PREFIXES,
        _CODEX_COMPACTION_META_KEY,
        _DRIFT_KEYS_MAX,
        _HELD_ASYNC_LAUNCH_TOOLS,
        _LAUNCH_SHAPED_WORDS,
        BACKGROUND_LAUNCH_LABELS_MAX,
        CODEX_COMPACTION_TITLE,
        BackgroundLaunchRecord,
        _is_launch_shaped,
        _printable_launch_label,
        _service_error_detail,
        is_unattributed_error_candidate,
        parse_background_launch,
        parse_claude_compaction_notice,
        parse_codex_compaction_update,
        prompt_is_adapter_command,
        unattributed_terminal_error,
        unrecognised_background_launch,
    )
    from kiro_crew.acp._dispatch_parts.permission_requests import (  # noqa: F401
        _DENY_BEHAVIORS,
        _DENY_OPTION_IDS,
        _KAS_NON_COMMAND_TOOLS,
        _LEGACY_OPTION_KIND,
        _PI_MCP_NAME_UNSAFE,
        _SPEC_OPTION_KINDS,
        _WORKSPACE_PATH_ARGUMENTS,
        GATE_ENVELOPE_MARKER,
        _anchored_in_workspace,
        _workspace_call_arguments,
        build_permission_event,
        gate_envelope,
        identified_mcp_call,
        is_mcp_tool_approval,
        kas_consent_tool,
        pi_mcp_title,
        reject_option_id,
    )
    from kiro_crew.acp._dispatch_parts.session_update_parser import (  # noqa: F401
        _MESSAGE_ID_MAX_CHARS,
        _build_tool_call_event,
        _build_tool_refinement_event,
        parse_message_id,
        parse_session_update,
        parse_text_chunk,
    )
    from kiro_crew.acp._dispatch_parts.surfaced_text import (  # noqa: F401
        _EDIT_CONTENT_KEYS,
        _REQUEST_ID_LOG_CAP,
        _REQUEST_ID_REDACT_INPUT_CAP,
        DIFF_TRUNCATION_MARK,
        UNSERIALISABLE_SIBLING_VALUE,
        _dumps_degraded,
        _loggable_request_id,
        _redact,
        derive_edit_diff,
        extract_tool_purpose,
        fits_tool_result,
        is_tool_purpose_key,
        make_unified_diff,
        redact_backend_text,
        redact_text,
        select_tool_title,
    )
    from kiro_crew.acp._dispatch_parts.tool_call_identity import (  # noqa: F401
        _ACP_SHELL_KIND,
        _CODEX_MCP_TOOL_CALL_MARKER,
        _HARNESS_TOOL_ID_RE,
        _MAX_HARNESS_TOOL_ID_LEN,
        _MCP_IDENTITY_META_CHANNELS,
        ToolCallIdentity,
        _harness_identifier,
        _kiro_mcp_server_name,
        _kiro_tool_name,
        _meta_identity,
        _meta_identity_full,
        _MetaIdentityChannel,
        _permission_tool_id,
        _str_field,
        classify_tool_call,
        harness_tool_name,
        is_shell_kind,
        meta_builtin_server_names,
        meta_builtin_shell,
    )
    from kiro_crew.acp._dispatch_parts.tool_call_provenance import (  # noqa: F401
        LIVENESS_DIGEST_PREFIX,
        MAX_ACTIVE_TOOL_CALLS,
        OVERFLOW_EVERY_SCOPE,
        TOOL_CALL_CACHE_MAX_ENTRIES,
        TOOL_CALL_CACHE_MAX_KEY_CHARS,
        TOOL_CALL_CACHE_MAX_PATH_CHARS,
        TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS,
        InFlightIdSet,
        _params_chars_for_admission,
        admit_tool_call_cache_entry,
        liveness_key,
        overflow_scope_key,
        release_tool_call_provenance,
        scope_overflowed,
        scoped_tool_cache_key,
    )
    from kiro_crew.acp._dispatch_parts.tool_result_events import (  # noqa: F401
        _INNER_ABSENT,
        _KNOWN_CONTENT_BLOCK_TYPES,
        _KNOWN_TOOL_CONTENT_TYPES,
        _MAX_SHAPE_DIAGNOSTIC,
        _MAX_SHAPE_KEYS,
        _build_tool_result_event,
        _mcp_content_text,
        _measure_tool_output,
        _rendered_key_names,
        _rendered_shape_type,
        _todo_payload,
        log_unrenderable_content,
        parse_todo_snapshot,
        tool_call_content_text,
        unrenderable_content_shapes,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    import difflib  # noqa: F401
    import hashlib  # noqa: F401
    import json  # noqa: F401
    import posixpath  # noqa: F401
    import re  # noqa: F401
    import unicodedata  # noqa: F401
    from collections.abc import (  # noqa: F401
        Iterable,
        Iterator,
    )
    from dataclasses import (  # noqa: F401
        dataclass,
        field,
    )
    from typing import (  # noqa: F401
        NamedTuple,
        cast,
    )


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


def parse_usage_cost(update: dict[str, Any]) -> float | None:
    """Parse a ``usage_update``'s session-cumulative cost into a validated float.

    The claude-agent-acp adapter reports billing as ``cost: {amount, currency}``
    on ``usage_update`` (session-cumulative). kiro-cli never sends the key, so
    the kiro path always reads None here. Same defensive posture as
    ``parse_usage_update`` (the other consumer of this frame): the value comes
    straight from the agent process, so a malformed shape (non-dict cost,
    str/list/bool amount, NaN/Infinity, negative) must degrade to "absent",
    never raise inside the prompt-turn dispatch path. Both consumers store
    the result in USD-denominated fields, so a present ``currency`` other
    than exact ``"USD"`` (ISO 4217 uppercase; an absent currency is accepted
    for adapters that omit it) also degrades the whole cost to absent rather
    than mislabeling a non-USD amount as USD. Flat-primary with a nested
    ``update.usage.cost`` fallback, mirroring ``parse_usage_update``.
    """
    if not isinstance(update, dict):
        return None
    cost = update.get("cost")
    if cost is None:
        nested = update.get("usage")
        if isinstance(nested, dict):
            cost = nested.get("cost")
    if not isinstance(cost, dict):
        return None
    currency = cost.get("currency")
    if currency is not None and currency != "USD":
        logger.debug("acp usage cost: non-USD currency %s, dropping cost", repr(currency)[:40])
        return None
    amount = _token_count(cost.get("amount"))
    if amount is None or amount < 0:
        return None
    return float(amount)


def parse_prompt_token_usage(result: Any) -> tuple[int, int, int, int] | None:
    """Parse a PromptResponse's turn-scoped token counts.

    The claude-agent-acp adapter reports per-turn token counts on the prompt
    RESPONSE (``inputTokens`` / ``outputTokens`` / ``cachedReadTokens`` /
    ``cachedWriteTokens``); kiro-cli's response carries only ``stopReason``.
    Returns ``(input, output, cache_read, cache_write)`` with each field
    validated via ``_token_count`` (bool excluded, finite) plus non-negative,
    coerced to int; an absent or malformed field reads 0. Returns None when
    NONE of the four keys is present, so the kiro path never touches the
    per-turn stats (harness parity: byte-identical behavior for a backend
    that sends no token counts). Flat-primary with a nested ``result.usage``
    fallback, mirroring ``parse_usage_update``'s dual-shape read.
    """
    if not isinstance(result, dict):
        return None
    nested = result.get("usage")
    nested = nested if isinstance(nested, dict) else {}
    keys = ("inputTokens", "outputTokens", "cachedReadTokens", "cachedWriteTokens")
    if not any(k in result or k in nested for k in keys):
        return None

    def _count(key: str) -> int:
        value = result.get(key, nested.get(key))
        n = _token_count(value)
        if n is None or n < 0:
            return 0
        return int(n)

    return _count(keys[0]), _count(keys[1]), _count(keys[2]), _count(keys[3])


# This module is the facade of the dispatch owners in ``kiro_crew.acp._dispatch_parts``,
# and every name is reached through it as ``kiro_crew.acp._dispatch.<name>``: a read is
# served by the owner that binds the name, and a write -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every owner holding it (:mod:`kiro_crew._facade_mirror`).
# Owners never import this module. The session handshake shapes, the metadata, refusal
# and usage parsers and the notification classifier stay here; new work for any other
# responsibility goes to the owner listed in :mod:`kiro_crew.acp._dispatch_parts`.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.acp._dispatch_parts.tool_call_identity",
    "kiro_crew.acp._dispatch_parts.surfaced_text",
    "kiro_crew.acp._dispatch_parts.tool_call_provenance",
    "kiro_crew.acp._dispatch_parts.adapter_signals",
    "kiro_crew.acp._dispatch_parts.tool_result_events",
    "kiro_crew.acp._dispatch_parts.permission_requests",
    "kiro_crew.acp._dispatch_parts.session_update_parser",
)
# The owners imported plainly as well, in _OWNERS order, so a type checker orders them
# before the modules that import this facade; the loop below is the mirror's own import.
# isort: off
from kiro_crew.acp._dispatch_parts import (  # noqa: E402,F401
    tool_call_identity as _tool_call_identity,
)
from kiro_crew.acp._dispatch_parts import (  # noqa: E402,F401
    surfaced_text as _surfaced_text,
)
from kiro_crew.acp._dispatch_parts import (  # noqa: E402,F401
    tool_call_provenance as _tool_call_provenance,
)
from kiro_crew.acp._dispatch_parts import (  # noqa: E402,F401
    adapter_signals as _adapter_signals,
)
from kiro_crew.acp._dispatch_parts import (  # noqa: E402,F401
    tool_result_events as _tool_result_events,
)
from kiro_crew.acp._dispatch_parts import (  # noqa: E402,F401
    permission_requests as _permission_requests,
)
from kiro_crew.acp._dispatch_parts import (  # noqa: E402,F401
    session_update_parser as _session_update_parser,
)

# isort: on
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name the module and its owners bind.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
