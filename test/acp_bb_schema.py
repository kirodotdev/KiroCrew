"""ACP v1 frame validator for the black-box conformance gate.

This validator is built on the **vendored upstream ACP v1 schema** — the files
copied verbatim from ``agentclientprotocol/agent-client-protocol`` at tag
``schema-v1.21.0`` (commit ``272bf799f35a258c6a4107a0410ed361e83683d3``) under
``test/conformance/vendor/acp-v1/`` — and imports **nothing** from
:mod:`kiro_crew`. Every closed vocabulary it checks against (method names, stop
reasons, session-update kinds, content-block types, permission-option kinds,
JSON-RPC error codes, negotiated protocol version) is derived from those pinned
files via :mod:`acp_v1_vendor`. That makes the oracle independent of Kiro Crew's
own :mod:`kiro_crew.acp.types` constants: if Kiro Crew drifts from the real
protocol, the frames it emits fail this gate rather than being rubber-stamped by
constants shared with the implementation.

Scope: JSON-RPC 2.0 envelope discipline + the ACP v1 baseline method/notification
shapes Kiro Crew implements, with all vocabularies keyed off the vendored schema.
It is intentionally strict about known-bad shapes and lenient about *extra*
optional keys (ACP permits namespaced extensions such as ``agentInfo``,
``messageId`` and ``_meta``), so a legitimate optional field never trips a false
failure.

Known limitation (see ``test/conformance/vendor/acp-v1/VENDOR.md``): no JSON
Schema Draft 2020-12 validator is installable offline and an unpinned dependency
is disallowed, so this performs vocabulary + structural checks rather than full
schema-object validation. When ``jsonschema`` (or the official Pydantic SDK)
becomes available, this file is the single seam to swap for schema-driven
validation — the harness already routes every frame through :func:`validate_frame`.
"""

from __future__ import annotations

from typing import Any, Callable

import acp_v1_vendor as acp

# ── wire literals this gate dispatches on ──
# Each is a real ACP v1 wire string; membership in the vendored vocabulary is
# asserted at import (below), so a typo or an upstream rename fails loudly here
# rather than silently skipping a shape check.
METHOD_INITIALIZE = "initialize"
METHOD_SESSION_NEW = "session/new"
METHOD_SESSION_LOAD = "session/load"
METHOD_SESSION_RESUME = "session/resume"
METHOD_SESSION_LIST = "session/list"
METHOD_PROMPT = "session/prompt"
METHOD_SESSION_UPDATE = "session/update"
METHOD_REQUEST_PERMISSION = "session/request_permission"
METHOD_SET_MODE = "session/set_mode"
METHOD_SET_CONFIG_OPTION = "session/set_config_option"

UPDATE_AGENT_MESSAGE_CHUNK = "agent_message_chunk"
UPDATE_AGENT_THOUGHT_CHUNK = "agent_thought_chunk"
UPDATE_USER_MESSAGE_CHUNK = "user_message_chunk"
UPDATE_TOOL_CALL = "tool_call"
UPDATE_TOOL_CALL_UPDATE = "tool_call_update"
UPDATE_CURRENT_MODE = "current_mode_update"
UPDATE_CONFIG_OPTION = "config_option_update"

# ── authoritative closed sets, from the vendored upstream schema ──
_ALLOWED_ERROR_CODES = acp.ALLOWED_JSONRPC_ERROR_CODES
_STOP_REASONS = acp.stop_reasons()
_SESSION_UPDATE_KINDS = acp.session_update_kinds()
_PERMISSION_OPTION_KINDS = acp.permission_option_kinds()
_CONTENT_BLOCK_TYPES = acp.content_block_types()
_ALL_METHODS = acp.all_methods()
_PROTOCOL_VERSION = acp.protocol_version()
_CONFIG_OPTION_CATEGORIES = acp.config_option_categories()

# The text-carrying session/update kinds whose ``content`` must be a text block.
_TEXT_UPDATE_KINDS = frozenset(
    {UPDATE_AGENT_MESSAGE_CHUNK, UPDATE_AGENT_THOUGHT_CHUNK, UPDATE_USER_MESSAGE_CHUNK}
)

# Fail fast at import if any dispatch literal is not part of the vendored ACP v1
# vocabulary — this is what keeps the gate honest against the pinned schema.
_DISPATCH_METHODS = frozenset(
    {
        METHOD_INITIALIZE,
        METHOD_SESSION_NEW,
        METHOD_SESSION_LOAD,
        METHOD_SESSION_RESUME,
        METHOD_SESSION_LIST,
        METHOD_PROMPT,
        METHOD_SESSION_UPDATE,
        METHOD_REQUEST_PERMISSION,
        METHOD_SET_MODE,
        METHOD_SET_CONFIG_OPTION,
    }
)
_unknown_methods = _DISPATCH_METHODS - _ALL_METHODS
assert not _unknown_methods, (
    f"conformance gate references methods absent from vendored ACP v1 meta.json: "
    f"{sorted(_unknown_methods)}"
)
_unknown_updates = _TEXT_UPDATE_KINDS | {
    UPDATE_TOOL_CALL,
    UPDATE_TOOL_CALL_UPDATE,
    UPDATE_CURRENT_MODE,
    UPDATE_CONFIG_OPTION,
}
_unknown_updates -= _SESSION_UPDATE_KINDS
assert not _unknown_updates, (
    f"conformance gate references session/update kinds absent from vendored ACP v1 "
    f"schema.json: {sorted(_unknown_updates)}"
)
# The reserved model-selector category Kiro Crew emits must exist in the vendored
# $defs/SessionConfigOptionCategory — a rename upstream fails here, not silently.
assert "model" in _CONFIG_OPTION_CATEGORIES, (
    f"'model' category absent from vendored ACP v1 $defs/SessionConfigOptionCategory: "
    f"{sorted(_CONFIG_OPTION_CATEGORIES)}"
)


class AcpSchemaError(AssertionError):
    """A frame emitted by the agent violated the vendored ACP v1 schema surface."""


def _is_json_rpc_id(value: Any) -> bool:
    """A JSON-RPC id is a string or a number, never a bool (an int subclass)."""
    if isinstance(value, bool):
        return False
    return isinstance(value, (str, int))


def _require(cond: bool, msg: str, frame: Any) -> None:
    if not cond:
        raise AcpSchemaError(f"{msg}\nframe={frame!r}")


def _is_text_content(content: Any) -> bool:
    return (
        isinstance(content, dict)
        and content.get("type") == "text"
        and isinstance(content.get("text"), str)
    )


def validate_envelope(frame: Any) -> str:
    """Validate the JSON-RPC 2.0 envelope. Returns the frame's role.

    Role is one of ``"result"``, ``"error"``, ``"request"``, ``"notification"``.
    Raises :class:`AcpSchemaError` on any envelope violation.
    """
    _require(isinstance(frame, dict), "frame is not a JSON object", frame)
    _require(frame.get("jsonrpc") == "2.0", "missing/invalid 'jsonrpc' (must be '2.0')", frame)

    has_method = "method" in frame and frame["method"] is not None
    has_id = "id" in frame
    has_result = "result" in frame
    has_error = "error" in frame

    if has_result or has_error:
        # A response: exactly one of result/error, an id present, no method.
        _require(not (has_result and has_error), "response has both result and error", frame)
        _require(not has_method, "response frame also carries a 'method'", frame)
        _require(has_id, "response frame is missing 'id'", frame)
        # id may be null only for an anonymous error (unparseable / no-id request).
        if frame.get("id") is not None:
            _require(_is_json_rpc_id(frame["id"]), "response 'id' is not a string/number", frame)
        if has_error:
            _validate_error_object(frame["error"], frame)
            return "error"
        return "result"

    _require(has_method, "frame is neither a response nor a request/notification", frame)
    _require(isinstance(frame["method"], str), "'method' must be a string", frame)
    if has_id:
        _require(_is_json_rpc_id(frame["id"]), "request 'id' is not a string/number", frame)
        return "request"
    return "notification"


def _validate_error_object(error: Any, frame: Any) -> None:
    _require(isinstance(error, dict), "error is not an object", frame)
    code = error.get("code")
    _require(isinstance(code, int) and not isinstance(code, bool), "error.code not an int", frame)
    _require(code in _ALLOWED_ERROR_CODES, f"error.code {code} not a permitted ACP code", frame)
    _require(isinstance(error.get("message"), str), "error.message not a string", frame)


def validate_frame(frame: Any, method_for_id: Callable[[Any], str | None]) -> str:
    """Validate a single agent-emitted frame against the vendored ACP v1 schema.

    ``method_for_id`` maps a response's ``id`` back to the client method that
    requested it, so a ``result`` can be checked against that method's expected
    shape. Returns the envelope role. Raises :class:`AcpSchemaError` on any
    violation.
    """
    role = validate_envelope(frame)
    if role == "result":
        method = method_for_id(frame.get("id"))
        _validate_result(method, frame["result"], frame)
    elif role == "error":
        # Envelope validation already checked the error object + code membership.
        # Which code is correct for which case is asserted by the tests.
        pass
    elif role == "request":
        _validate_agent_request(frame["method"], frame.get("params"), frame)
    else:  # notification
        _validate_notification(frame["method"], frame.get("params"), frame)
    return role


def _validate_result(method: str | None, result: Any, frame: Any) -> None:
    if method == METHOD_INITIALIZE:
        _validate_initialize_result(result, frame)
    elif method == METHOD_SESSION_NEW:
        _require(isinstance(result, dict), "session/new result not an object", frame)
        _require(
            isinstance(result.get("sessionId"), str) and result["sessionId"],
            "session/new result missing non-empty 'sessionId'",
            frame,
        )
        _validate_selector_fields(result, frame)
    elif method in (METHOD_SESSION_LOAD, METHOD_SESSION_RESUME):
        _require(isinstance(result, dict), f"{method} result not an object", frame)
        _validate_selector_fields(result, frame)
    elif method == METHOD_SESSION_LIST:
        _validate_session_list_result(result, frame)
    elif method == METHOD_SET_MODE:
        # SetSessionModeResponse is an (optionally _meta-carrying) empty object.
        _require(isinstance(result, dict), "session/set_mode result not an object", frame)
    elif method == METHOD_SET_CONFIG_OPTION:
        _validate_set_config_option_result(result, frame)
    elif method == METHOD_PROMPT:
        _require(isinstance(result, dict), "session/prompt result not an object", frame)
        stop = result.get("stopReason")
        _require(
            stop in _STOP_REASONS,
            f"session/prompt stopReason {stop!r} not in vendored ACP v1 $defs/StopReason "
            f"{sorted(_STOP_REASONS)}",
            frame,
        )
    # Unknown/other method results: envelope-valid is sufficient.


def _validate_selector_fields(result: dict[str, Any], frame: Any) -> None:
    """Validate optional ``modes``/``configOptions`` on a lifecycle result.

    Both are optional in the vendored NewSession/LoadSession/ResumeSession
    responses; when present they must match SessionModeState / [SessionConfigOption].
    """
    modes = result.get("modes")
    if modes is not None:
        _validate_mode_state(modes, frame)
    config_options = result.get("configOptions")
    if config_options is not None:
        _require(isinstance(config_options, list), "configOptions not an array", frame)
        for opt in config_options:
            _validate_config_option(opt, frame)


def _validate_mode_state(modes: Any, frame: Any) -> None:
    """Validate a vendored ``$defs/SessionModeState`` object."""
    _require(isinstance(modes, dict), "modes (SessionModeState) not an object", frame)
    _require(
        isinstance(modes.get("currentModeId"), str) and modes["currentModeId"],
        "SessionModeState missing non-empty 'currentModeId'",
        frame,
    )
    available = modes.get("availableModes")
    _require(
        isinstance(available, list) and bool(available),
        "SessionModeState missing non-empty 'availableModes'",
        frame,
    )
    for mode in available:
        _require(isinstance(mode, dict), "availableModes item not an object", frame)
        _require(isinstance(mode.get("id"), str) and mode["id"], "SessionMode missing 'id'", frame)
        _require(isinstance(mode.get("name"), str), "SessionMode missing 'name'", frame)


def _validate_config_option(opt: Any, frame: Any) -> None:
    """Validate a vendored ``$defs/SessionConfigOption`` (select or boolean)."""
    _require(isinstance(opt, dict), "SessionConfigOption not an object", frame)
    _require(
        isinstance(opt.get("id"), str) and opt["id"], "SessionConfigOption missing 'id'", frame
    )
    _require(isinstance(opt.get("name"), str), "SessionConfigOption missing 'name'", frame)
    otype = opt.get("type")
    _require(
        otype in ("select", "boolean"),
        f"SessionConfigOption 'type' {otype!r} not select|boolean",
        frame,
    )
    # category is optional; ACP allows any string (reserved consts + free 'other').
    category = opt.get("category")
    if category is not None:
        _require(isinstance(category, str), "SessionConfigOption category not a string", frame)
    if otype == "select":
        _require(
            isinstance(opt.get("currentValue"), str),
            "select SessionConfigOption missing string 'currentValue'",
            frame,
        )
        options = opt.get("options")
        _require(
            isinstance(options, list),
            "select SessionConfigOption missing 'options' array",
            frame,
        )
        for entry in options:
            _require(isinstance(entry, dict), "select option entry not an object", frame)
            if "group" in entry:  # grouped: {group, name, options:[{value,name}]}
                _require(isinstance(entry.get("group"), str), "group missing 'group'", frame)
                _require(isinstance(entry.get("name"), str), "group missing 'name'", frame)
                subs = entry.get("options")
                _require(isinstance(subs, list), "group missing 'options' array", frame)
                for sub in subs:
                    _require(
                        isinstance(sub, dict)
                        and isinstance(sub.get("value"), str)
                        and isinstance(sub.get("name"), str),
                        "grouped select option value malformed",
                        frame,
                    )
            else:  # ungrouped: {value, name}
                _require(
                    isinstance(entry.get("value"), str),
                    "select option missing string 'value'",
                    frame,
                )
                _require(
                    isinstance(entry.get("name"), str), "select option value missing 'name'", frame
                )
    else:  # boolean
        _require(
            isinstance(opt.get("currentValue"), bool),
            "boolean SessionConfigOption missing bool 'currentValue'",
            frame,
        )


def _validate_set_config_option_result(result: Any, frame: Any) -> None:
    """Validate a vendored ``SetSessionConfigOptionResponse`` (configOptions required)."""
    _require(isinstance(result, dict), "session/set_config_option result not an object", frame)
    options = result.get("configOptions")
    _require(
        isinstance(options, list),
        "session/set_config_option result missing 'configOptions' array",
        frame,
    )
    for opt in options:
        _validate_config_option(opt, frame)


def _validate_initialize_result(result: Any, frame: Any) -> None:
    _require(isinstance(result, dict), "initialize result not an object", frame)
    version = result.get("protocolVersion")
    _require(
        version == _PROTOCOL_VERSION and isinstance(version, int) and not isinstance(version, bool),
        f"initialize protocolVersion must be int {_PROTOCOL_VERSION} (the vendored ACP v1 "
        "wire version); the agent must never echo an unsupported offered version",
        frame,
    )
    caps = result.get("agentCapabilities")
    _require(isinstance(caps, dict), "agentCapabilities not an object", frame)
    _require(isinstance(caps.get("loadSession"), bool), "loadSession capability not a bool", frame)
    session_caps = caps.get("sessionCapabilities")
    if session_caps is not None:
        # ACP permits a capability map; be lenient about which capability keys
        # appear (they are additive), strict that each maps to an object.
        _require(isinstance(session_caps, dict), "sessionCapabilities not an object", frame)
        for key, val in session_caps.items():
            _require(isinstance(val, dict), f"sessionCapabilities[{key}] not an object", frame)
    info = result.get("agentInfo")
    if info is not None:
        _require(isinstance(info, dict), "agentInfo not an object", frame)
        _require(isinstance(info.get("name"), str), "agentInfo.name not a string", frame)
        _require(isinstance(info.get("version"), str), "agentInfo.version not a string", frame)


def _validate_session_list_result(result: Any, frame: Any) -> None:
    _require(isinstance(result, dict), "session/list result not an object", frame)
    sessions = result.get("sessions")
    _require(isinstance(sessions, list), "session/list result missing 'sessions' array", frame)
    for item in sessions:
        _require(isinstance(item, dict), "session/list item not an object", frame)
        _require(
            isinstance(item.get("sessionId"), str) and item["sessionId"],
            "session/list item missing non-empty 'sessionId'",
            frame,
        )
        if "cwd" in item and item["cwd"] is not None:
            _require(isinstance(item["cwd"], str), "session/list item cwd not a string", frame)
        if "title" in item and item["title"] is not None:
            _require(isinstance(item["title"], str), "session/list item title not a string", frame)
        if "updatedAt" in item and item["updatedAt"] is not None:
            _require(
                isinstance(item["updatedAt"], str),
                "session/list item updatedAt not a string",
                frame,
            )


def _validate_notification(method: str, params: Any, frame: Any) -> None:
    if method != METHOD_SESSION_UPDATE:
        # Any other agent->client notification is allowed by envelope alone.
        return
    _require(isinstance(params, dict), "session/update params not an object", frame)
    _require(
        isinstance(params.get("sessionId"), str) and params["sessionId"],
        "session/update missing non-empty 'sessionId'",
        frame,
    )
    update = params.get("update")
    _require(isinstance(update, dict), "session/update 'update' not an object", frame)
    kind = update.get("sessionUpdate")
    _require(
        kind in _SESSION_UPDATE_KINDS,
        f"session/update kind {kind!r} not in vendored ACP v1 $defs/SessionUpdate "
        f"{sorted(_SESSION_UPDATE_KINDS)}",
        frame,
    )
    if kind in _TEXT_UPDATE_KINDS:
        _require(
            _is_text_content(update.get("content")),
            f"{kind} content is not a text block",
            frame,
        )
    elif kind == UPDATE_TOOL_CALL:
        _require(isinstance(update.get("toolCallId"), str), "tool_call missing toolCallId", frame)
        _require(isinstance(update.get("title"), str), "tool_call missing title", frame)
        _require(isinstance(update.get("kind"), str), "tool_call missing kind", frame)
        _require(isinstance(update.get("status"), str), "tool_call missing status", frame)
    elif kind == UPDATE_TOOL_CALL_UPDATE:
        _require(
            isinstance(update.get("toolCallId"), str),
            "tool_call_update missing toolCallId",
            frame,
        )
        _require(isinstance(update.get("status"), str), "tool_call_update missing status", frame)
    elif kind == UPDATE_CURRENT_MODE:
        _require(
            isinstance(update.get("currentModeId"), str) and update["currentModeId"],
            "current_mode_update missing non-empty 'currentModeId'",
            frame,
        )
    elif kind == UPDATE_CONFIG_OPTION:
        options = update.get("configOptions")
        _require(
            isinstance(options, list),
            "config_option_update missing 'configOptions' array",
            frame,
        )
        for opt in options:
            _validate_config_option(opt, frame)


def _validate_agent_request(method: str, params: Any, frame: Any) -> None:
    _require(
        method == METHOD_REQUEST_PERMISSION,
        f"unexpected agent->client request method {method!r}",
        frame,
    )
    _require(isinstance(params, dict), "request_permission params not an object", frame)
    _require(
        isinstance(params.get("sessionId"), str) and params["sessionId"],
        "request_permission missing non-empty 'sessionId'",
        frame,
    )
    tool_call = params.get("toolCall")
    _require(isinstance(tool_call, dict), "request_permission toolCall not an object", frame)
    _require(isinstance(tool_call.get("toolCallId"), str), "toolCall missing toolCallId", frame)
    _require(isinstance(tool_call.get("title"), str), "toolCall missing title", frame)
    _require(isinstance(tool_call.get("kind"), str), "toolCall missing kind", frame)
    if "content" in tool_call and tool_call["content"] is not None:
        _require(isinstance(tool_call["content"], list), "toolCall content not an array", frame)
    options = params.get("options")
    _require(
        isinstance(options, list) and bool(options), "request_permission missing options", frame
    )
    for opt in options:
        _require(isinstance(opt, dict), "permission option not an object", frame)
        # ACP: optionId is a free-form non-empty string; the closed vocabulary is
        # the option KIND ($defs/PermissionOptionKind).
        _require(
            isinstance(opt.get("optionId"), str) and opt["optionId"],
            "permission option missing non-empty optionId",
            frame,
        )
        _require(
            opt.get("kind") in _PERMISSION_OPTION_KINDS,
            f"permission option kind {opt.get('kind')!r} not in vendored ACP v1 "
            f"$defs/PermissionOptionKind {sorted(_PERMISSION_OPTION_KINDS)}",
            frame,
        )
        _require(isinstance(opt.get("name"), str), "permission option missing name", frame)
