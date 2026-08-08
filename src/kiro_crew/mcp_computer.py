"""MCP server exposing the computer-use tools — a THIN SHIM over the gateway.

Runs as ``kirocrew mcp-computer``: kiro-cli spawns it as a child process and calls
tools via JSON-RPC over stdio. Unlike ``mcp_cron`` / ``mcp_core``, this server
implements **nothing**. It

1. resolves the caller's session identity STRICTLY, and
2. forwards the call to the gateway over loopback with the internal-secret
   handshake — the same pattern ``mcp_core`` already uses for its own gateway
   round-trips.

All governance evaluation, all accessibility / screen-capture work, and all SEL
auditing happen IN THE GATEWAY (``computer_use/tools.py`` →
``computer_use/gate.py``). Three reasons, each load-bearing:

* **The authorization point must be fail-closed.**
  ``hooks._governance_denial`` — the PreToolUse gate — is fail-**OPEN** by
  deliberate repo policy (a governance glitch must not wedge every tool call on
  every surface), so it cannot be the sole authorization point for a surface that
  can read a password field's ``AXValue``. The authoritative gate
  (``gate.require_computer_use``) fails CLOSED and needs the OS-resolved app
  identity and the addressed element's role — which only the gateway-side tool
  body has.
* **Governance and the audit trail live where the platform context is composed.**
  The ceiling, the profile store and the SEL trust root are all gateway state.
* **No native code in this process.** This module imports no ctypes, loads no
  framework, and touches no accessibility API — so a driver fault can never take
  down the process kiro-cli is talking to, and this file is byte-identical in
  behavior on macOS, Linux and Windows.

Identity is resolved with :func:`mcp_core._resolve_session_key_strict` — the env
var, or ``KIROCREW_HOST_PID`` plus the HMAC sidecar signed with the
keystone-protected ``sel_hmac.key``. The lenient resolver is deliberately NOT
used: it walks ``/proc`` ancestors over ``session_pid_<pid>.txt``, which
``mcp_core`` itself documents as "agent-writable and therefore forgeable".

**An unresolved key is NOT a refusal.** It is forwarded empty and the call proceeds.
Neither accepted source exists for a GUI-launched kiro-cli on macOS —
``KIROCREW_SESSION_KEY`` is injected only by the ACP spawn path
(``acp/client.py``) and ``KIROCREW_HOST_PID`` only by the Linux sandbox launcher
(``sandbox.py``) — so gating on identity made the feature unusable on its only
supported platform. The unattended-surface rule was removed by product decision;
"we cannot name the session" must not become "you may not drive the desktop". What
is lost is audit ATTRIBUTION, not a control: the trail records an empty key, which
is honest, where the lenient walk would have recorded a forgeable one.

Tool visibility follows the keystone primary enable: ``tools/list`` returns ``[]``
while computer use is off, so a disabled feature is invisible to the model rather
than a set of tools that always refuse.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
import urllib.request
from typing import Any

from kiro_crew.computer_use import enable_state
from kiro_crew.computer_use.types import (
    CLICK_METHOD_ACCESSIBILITY,
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_AUTO,
    CLICK_METHOD_GLOBAL,
    CLICK_METHODS,
    ERROR_PREFIX,
    MAX_ACTION_LEN,
    MAX_CLICK_COUNT,
    MAX_ELEMENT_INDEX,
    MAX_KEY_LEN,
    MAX_SCREEN_COORD,
    MAX_SCROLL_PAGES,
    MAX_TEXT_LIMIT,
    MAX_TREE_DEPTH_LIMIT,
    MAX_TREE_NODES_LIMIT,
    MAX_TYPE_TEXT_LEN,
    MIN_CLICK_COUNT,
    MIN_SCREEN_COORD,
    MIN_SCROLL_PAGES,
    MOUSE_BUTTONS,
    REFUSAL_DISABLED,
    SCROLL_DIRECTIONS,
    TOOL_CLICK,
    TOOL_DRAG,
    TOOL_END_TURN,
    TOOL_GET_STATE,
    TOOL_LIST_APPS,
    TOOL_PERFORM_ACTION,
    TOOL_PRESS_KEY,
    TOOL_SCROLL,
    TOOL_SET_VALUE,
    TOOL_TYPE_TEXT,
)
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mcp_core import (
    _API,
    _http_error_body,
    _internal_secret,
    _resolve_session_key_strict,
    _session_key_header_error,
)
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.validation import MCP_COMPUTER_SCHEMAS, ValidationError, validate_tool_args

logger = logging.getLogger(__name__)

SERVER_NAME = "kirocrew-computer"
SERVER_VERSION = "1.0.0"
SESSION_KIND = "mcp_computer"

# The gateway route this shim forwards to. Loopback + ``X-Internal-Secret``; the
# gateway handler re-derives nothing from the request body except the tool name
# and arguments, and takes the session identity from the header.
INVOKE_PATH = "/api/computer-use/invoke"
# Generous: a first accessibility walk of an Electron app pays the
# ``AXManualAccessibility`` opt-in (~2s) on top of the walk itself, and a
# mutating tool does a drift walk, the action, and a re-walk. Still bounded, so a
# hung target app surfaces as an error instead of an indefinitely blocked tool
# call.
INVOKE_TIMEOUT_SECS = 90.0

# Refusals this shim produces on its own. Everything else is the gateway's text.
# NOTE: there is deliberately NO identity refusal here — an unresolvable session
# key proceeds with an empty identity. See the strict-resolver comment in
# ``_call_tool_inner``.
ERR_GATEWAY_UNREACHABLE = (
    "the KiroCrew gateway is not reachable, so computer use cannot run "
    "(every computer-use action is evaluated and audited in the gateway): {detail}"
)

# Identity used when neither accepted source resolves — which is the NORMAL case on
# macOS, the only platform with a driver (``KIROCREW_SESSION_KEY`` is injected only
# by the ACP spawn path and ``KIROCREW_HOST_PID`` only by the Linux sandbox
# launcher).
#
# Why this exists rather than an empty string: ``SnapshotIndex`` namespaces its
# entries by
# ``(session_key, window_key)``, so EVERY unresolved session shared the single
# ``("", window)`` slot. Two concurrent macOS sessions observing the same window
# therefore overwrote each other's element indices — and each one's own
# fingerprint check would still pass, because the two trees are of the same window.
# The result is a wrong-target action with nothing reporting it, which is the worst
# class this feature can produce.
#
# The fix is a per-PROCESS identity, not a refusal. kiro-cli spawns one shim process
# per session, so the shim's own pid separates the namespaces exactly as far as the
# sessions are actually separate — and it does so without reinstating the
# unattended-surface refusal that was removed by product decision. It is
# deliberately NOT presented as trustworthy attribution: the prefix names it as
# unresolved so an audit reader cannot mistake a pid for a session identity.
UNRESOLVED_SESSION_PREFIX = "unresolved:"


def _unresolved_session_key() -> str:
    """A per-PROCESS session identity for a session we could not name.

    ``unresolved:<pid>`` of THIS shim process. kiro-cli spawns one shim per session,
    so the pid separates two unresolved sessions exactly as far as they really are
    separate — which is what keeps ``SnapshotIndex``'s ``(session_key, window_key)``
    namespace from aliasing them onto one entry and letting one session's action
    resolve against another's element indices.

    Read at CALL time rather than captured at import: a ``fork``ed child would
    otherwise inherit the parent's string and re-alias with it, which is the exact
    failure this is here to prevent.

    Never presented as trustworthy attribution — the prefix says so. This is a
    namespace separator, not an authenticated identity; a genuine identity still
    comes only from the two sources ``_resolve_session_key_strict`` accepts.
    """
    return f"{UNRESOLVED_SESSION_PREFIX}{os.getpid()}"


def _list_tools() -> list[dict[str, Any]]:
    """Return the MCP tool definitions, or ``[]`` while the feature is disabled.

    Hiding the tools rather than advertising nine that always refuse is a
    deliberate product decision: kiro-cli caches ``tools/list`` once per session,
    so a disabled feature would otherwise spend context on capabilities the model
    can never use and invite retry loops.

    Fail-CLOSED (hidden) on any error reading the keystone: an unreadable primary
    enable means "not proven on".
    """
    try:
        if not enable_state.is_enabled():
            return []
    except Exception:
        logger.debug("computer-use enable-state probe failed; hiding tools", exc_info=True)
        return []
    return _tool_definitions()


def _coord_prop(description: str) -> dict[str, Any]:
    """A screen-coordinate property, in the top-left convention.

    Advertised as ``number`` (not ``integer``) so a model can pass the fractional
    centre of an element's frame without a rejection; the bound mirrors
    ``validation``'s, which is the enforcement point.
    """
    return {
        "type": "number",
        "minimum": MIN_SCREEN_COORD,
        "maximum": MAX_SCREEN_COORD,
        "description": description,
    }


def _tool_definitions() -> list[dict[str, Any]]:
    """The ten tool definitions.

    Bounds mirror ``validation.MCP_COMPUTER_SCHEMAS`` (which is the enforcement
    point — this is advertisement, so a mismatch would only produce a confusing
    error rather than a hole). Descriptions carry the operating discipline the
    model must follow, because there is no other place it will read it: call
    ``computer_get_state`` first, address by ``element_index``, read the
    screenshot only if the tree is insufficient.
    """
    app_prop = {
        "type": "string",
        "description": (
            "Application to target: a display name ('Finder', 'Preview') or a "
            f"bundle id ('com.apple.finder'). Call {TOOL_LIST_APPS} to see what "
            "is on screen."
        ),
    }
    index_prop = {
        "type": "integer",
        "minimum": 0,
        "maximum": MAX_ELEMENT_INDEX,
        "description": (
            f"The element's index from the most recent {TOOL_GET_STATE} result. "
            "Refused if the window changed since then — call "
            f"{TOOL_GET_STATE} again."
        ),
    }
    button_prop = {
        "type": "string",
        "enum": list(MOUSE_BUTTONS),
        "description": "Mouse button. Defaults to left.",
    }
    return [
        {
            "name": TOOL_LIST_APPS,
            "description": (
                "List desktop applications that currently have an on-screen "
                "window, with their bundle id and window title. Start here when "
                "you do not already know how the user names the app."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": TOOL_GET_STATE,
            "description": (
                "Read one application's window as an indexed accessibility tree "
                "(roles, titles, values, available actions) plus, when permitted, "
                "the path to a compressed screenshot. CALL THIS FIRST every turn "
                "before any click/type/scroll: the other tools address elements by "
                "the index this returns and refuse a stale one. The tree is the "
                "primary channel — read the screenshot file only when the tree is "
                "insufficient. Password fields are reported as present but their "
                "contents are never readable, and a window containing one is not "
                "captured."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "app": app_prop,
                    "text_limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_TEXT_LIMIT,
                        "description": "Max characters per title/value (default 500).",
                    },
                    "max_tree_nodes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_TREE_NODES_LIMIT,
                        "description": "Max elements to return (default 1200).",
                    },
                    "max_tree_depth": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_TREE_DEPTH_LIMIT,
                        "description": "Max tree depth to walk (default 64).",
                    },
                    "screenshot": {
                        "type": "boolean",
                        "description": (
                            "Attach a screenshot path. Defaults to the user's "
                            "setting; may be refused by policy."
                        ),
                    },
                },
                "required": ["app"],
            },
        },
        {
            "name": TOOL_CLICK,
            "description": (
                "Click in an application window. Give EITHER element_index (from "
                f"{TOOL_GET_STATE}) OR both x and y screen coordinates — not both, "
                "and not neither. Prefer element_index: it activates the control "
                "directly through accessibility, needs no pixel measurement and "
                "never moves the mouse pointer. Use x/y for canvases, maps and "
                "custom-drawn UI that exposes no addressable element."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "app": app_prop,
                    "element_index": dict(
                        index_prop,
                        description=(
                            "The control to press, from the most recent "
                            f"{TOOL_GET_STATE} result. Omit when clicking x/y."
                        ),
                    ),
                    "x": _coord_prop(
                        "X screen coordinate. Requires y. Omit when using element_index."
                    ),
                    "y": _coord_prop(
                        "Y screen coordinate. Requires x. Omit when using element_index."
                    ),
                    "click_count": {
                        "type": "integer",
                        "minimum": MIN_CLICK_COUNT,
                        "maximum": MAX_CLICK_COUNT,
                        "description": (
                            "1 for a single click, 2 for a double click, 3 for a "
                            "triple click. Defaults to 1."
                        ),
                    },
                    "mouse_button": button_prop,
                    "click_method": {
                        "type": "string",
                        "enum": list(CLICK_METHODS),
                        "description": (
                            f"How the click is delivered. '{CLICK_METHOD_AUTO}' "
                            f"(default) uses '{CLICK_METHOD_ACCESSIBILITY}' when you "
                            f"gave an element_index and '{CLICK_METHOD_APP_POST}' "
                            f"when you gave x/y. '{CLICK_METHOD_ACCESSIBILITY}' "
                            "presses the control and requires element_index. "
                            f"'{CLICK_METHOD_APP_POST}' sends a click at x/y to the "
                            "target app WITHOUT moving the pointer — correct for a "
                            f"background window. '{CLICK_METHOD_GLOBAL}' MOVES THE "
                            "USER'S REAL MOUSE POINTER and clicks there; it is off "
                            "by default and refused unless the user enabled it and "
                            "policy permits it, so only ask for it when a click "
                            "must be physically real."
                        ),
                    },
                },
                "required": ["app"],
            },
        },
        {
            "name": TOOL_DRAG,
            "description": (
                "Drag from one screen point to another inside an application — for "
                "canvas strokes, sliders, range selections and reordering. "
                "Coordinate-only: there is no element form, because a drag's "
                "meaning is the path between the two points. The pointer is not "
                f"moved unless you ask for click_method '{CLICK_METHOD_GLOBAL}'."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "app": app_prop,
                    "from_x": _coord_prop("Start X screen coordinate."),
                    "from_y": _coord_prop("Start Y screen coordinate."),
                    "to_x": _coord_prop("End X screen coordinate."),
                    "to_y": _coord_prop("End Y screen coordinate."),
                    "mouse_button": button_prop,
                    "click_method": {
                        "type": "string",
                        "enum": list(CLICK_METHODS),
                        "description": (
                            f"'{CLICK_METHOD_AUTO}' (default) and "
                            f"'{CLICK_METHOD_APP_POST}' send the drag to the target "
                            "app without moving the pointer. "
                            f"'{CLICK_METHOD_GLOBAL}' MOVES THE USER'S REAL MOUSE "
                            "POINTER along the path and needs their opt-in. "
                            f"'{CLICK_METHOD_ACCESSIBILITY}' cannot express a drag "
                            "and is refused."
                        ),
                    },
                },
                "required": ["app", "from_x", "from_y", "to_x", "to_y"],
            },
        },
        {
            "name": TOOL_TYPE_TEXT,
            "description": (
                "Type text into an application, as keystrokes. Name the target "
                f"field with element_index from the most recent {TOOL_GET_STATE} "
                "result. Refused for password fields and for text that looks like "
                "a destructive shell command."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "app": app_prop,
                    "text": {
                        "type": "string",
                        "maxLength": MAX_TYPE_TEXT_LEN,
                        "description": "The literal text to type.",
                    },
                    # REQUIRED, matching ``MCP_COMPUTER_SCHEMAS``. Keystrokes go
                    # only to an element KiroCrew can inspect: with no index there
                    # is no role/subrole for the always-on secure-target refusal to
                    # check, so "type into whatever is focused" could land in a
                    # focused password box. The advertised schema has to say so —
                    # a tool whose "required" list is looser than the validator's
                    # teaches the model a call shape that is always refused.
                    "element_index": dict(
                        index_prop,
                        description=(
                            "The field to type into, from the most recent "
                            f"{TOOL_GET_STATE} result."
                        ),
                    ),
                },
                "required": ["app", "text", "element_index"],
            },
        },
        {
            "name": TOOL_PRESS_KEY,
            "description": (
                "Send one keyboard shortcut to an application, e.g. 'cmd+s', "
                "'shift+tab', 'escape', 'cmd+shift+a'. Modifiers: cmd/command, "
                "shift, option/alt, control/ctrl, fn. One shortcut per call."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "app": app_prop,
                    "key": {
                        "type": "string",
                        "maxLength": MAX_KEY_LEN,
                        "description": "Key spec, e.g. 'cmd+s' or 'escape'.",
                    },
                    # REQUIRED, for the same reason as computer_type_text above:
                    # the validator requires it, so omitting it here advertises a
                    # schema-conforming call that is refused every single time.
                    "element_index": dict(
                        index_prop,
                        description=(
                            "The element to send the shortcut to, from the most "
                            f"recent {TOOL_GET_STATE} result."
                        ),
                    ),
                },
                "required": ["app", "key", "element_index"],
            },
        },
        {
            "name": TOOL_SET_VALUE,
            "description": (
                "Set a field's value directly, replacing its contents without "
                "keystrokes. Faster and more reliable than typing for long text, "
                "but it does not fire per-keystroke handlers, so prefer "
                f"{TOOL_TYPE_TEXT} for search boxes and autocompletes. Refused "
                "for password fields."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "app": app_prop,
                    "element_index": index_prop,
                    "value": {
                        "type": "string",
                        "maxLength": MAX_TYPE_TEXT_LEN,
                        "description": "The new value.",
                    },
                },
                "required": ["app", "element_index", "value"],
            },
        },
        {
            "name": TOOL_SCROLL,
            "description": (
                "Scroll a scrollable element by whole pages. Address the "
                "scroll area (or a list/table) by its index."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "app": app_prop,
                    "element_index": index_prop,
                    "direction": {
                        "type": "string",
                        "enum": list(SCROLL_DIRECTIONS),
                        "description": "Scroll direction.",
                    },
                    "pages": {
                        "type": "number",
                        "minimum": MIN_SCROLL_PAGES,
                        "maximum": MAX_SCROLL_PAGES,
                        "description": "Pages to scroll (default 1).",
                    },
                },
                "required": ["app", "element_index", "direction"],
            },
        },
        {
            "name": TOOL_PERFORM_ACTION,
            "description": (
                "Perform a named accessibility action on an element — use this "
                "for the actions the element advertises in its "
                f"{TOOL_GET_STATE} line (e.g. 'AXShowMenu' to open a context "
                "menu, 'AXIncrement' on a stepper). Prefer "
                f"{TOOL_CLICK} for a plain press."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "app": app_prop,
                    "element_index": index_prop,
                    "action": {
                        "type": "string",
                        "maxLength": MAX_ACTION_LEN,
                        "description": (
                            "An action name the element advertised, e.g. 'AXShowMenu'."
                        ),
                    },
                },
                "required": ["app", "element_index", "action"],
            },
        },
        {
            "name": TOOL_END_TURN,
            "description": (
                "Release every cached window state when you are finished driving "
                "the desktop. Optional housekeeping — cached state also expires "
                "on its own — but calling it makes the next turn start from a "
                "fresh read instead of a refusal."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against the schema. Returns cleaned args.

    Unlike the cron/core servers there is NO raw pass-through fallback: an
    unregistered tool name is rejected. A computer-use tool reaching a handler
    with unvalidated arguments could synthesize input into a live window, and the
    gateway re-validates anyway — so refusing here just makes the failure early
    and legible.
    """
    schema = MCP_COMPUTER_SCHEMAS.get(name)
    if schema is None:
        raise ValidationError("name", f"unknown computer-use tool '{name}'")
    return validate_tool_args(args, schema)


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    """Validate and forward one tool call, with the standard SEL invocation log."""
    return call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        session_key=SESSION_KIND,
        downstream_service=SERVER_NAME,
    )


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    """Forward one validated call to the gateway. All decisions happen there."""
    # Re-checked here as well as in ``_list_tools``: kiro-cli caches the tool list
    # for the life of a session, so a session that started while the feature was
    # enabled keeps offering the tools after the operator turns it off. The
    # gateway checks it too (it is the authoritative gate); this just answers with
    # the actionable message instead of a generic refusal.
    try:
        if not enable_state.is_enabled():
            return f"{ERROR_PREFIX}{REFUSAL_DISABLED}"
    except Exception:
        logger.debug("computer-use enable-state probe failed; refusing", exc_info=True)
        return f"{ERROR_PREFIX}{REFUSAL_DISABLED}"

    # STRICT identity, but NOT a gate. An unresolvable key is passed through as
    # empty rather than refused: the unattended-surface rule was removed by product
    # decision, so "we could not name the session" must not become "you may not
    # drive the desktop". On macOS neither accepted source is even available to a
    # GUI-launched kiro-cli (``KIROCREW_SESSION_KEY`` is injected only by the ACP
    # spawn path, and ``KIROCREW_HOST_PID`` only by the Linux sandbox launcher), so
    # refusing here made the whole feature unusable on its only supported platform.
    #
    # Still the STRICT resolver, and deliberately: the lenient one walks a file
    # ``mcp_core`` itself documents as "agent-writable and therefore forgeable", and
    # an empty audit identity is honest where a forged one is a lie. What the audit
    # loses is attribution, which is worth less than the feature working.
    session_key = _resolve_session_key_strict() or _unresolved_session_key()
    header_err = _session_key_header_error(session_key)
    if header_err:
        return f"{ERROR_PREFIX}{header_err}"

    payload = _invoke(session_key, name, args)
    # ``text`` is the SUCCESS-AND-REFUSAL channel: the gateway answers 200 with
    # ``{"text": "..."}`` for both, because a computer-use refusal is a TOOL RESULT
    # ("Error: ...", which the SEL layer classifies as failed) rather than a
    # transport failure the model cannot reason about. ``error`` appears only for a
    # malformed request (4xx) or a transport problem this shim synthesized, so it is
    # checked second — a body carrying both would be reporting a real result.
    text = payload.get("text")
    if isinstance(text, str) and text:
        return text
    error = payload.get("error")
    if error:
        # The gateway's refusals are already model-facing prose that deliberately
        # does not disclose the ceiling's contents (the policy file is on the
        # keystone precisely so the agent cannot read it). Prefixed once here so
        # the SEL outcome classification in ``call_tool_with_logging`` sees a
        # failure.
        detail = str(error)
        return detail if detail.startswith(ERROR_PREFIX) else f"{ERROR_PREFIX}{detail}"
    return f"{ERROR_PREFIX}the gateway returned no result for '{name}'"


def _invoke(session_key: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """POST one tool call to the gateway. Returns the decoded JSON body.

    Returns ``{"error": ...}`` on any transport failure rather than raising: an
    exception escaping here would be caught by the stdio loop's generic handler
    and reported as an opaque internal error, where an unreachable gateway is
    both diagnosable and actionable.

    The body carries the resolved session key as well as the header. The HEADER is
    what the auth middleware sees; the BODY field is what the handler threads into
    the dispatcher as the calling surface. Neither is an authorization claim the
    gateway trusts on its own — the trust comes from the loopback local-secret
    handshake plus the STRICT resolution above, which has already refused an empty
    key before this function is reached.
    """
    body = json.dumps(
        {"tool": name, "args": args, "session_key": session_key, "agent": "", "app": ""}
    ).encode()
    request = urllib.request.Request(
        f"{_API}{INVOKE_PATH}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": _internal_secret(),
            "X-Session-Key": session_key,
        },
        method="POST",
    )
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- URL is the loopback gateway (_API from dashboard.url config) + a fixed internal path; never agent-controlled  # noqa: E501
        with loopback_urlopen(request, timeout=INVOKE_TIMEOUT_SECS) as response:
            decoded = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return _http_error_body(exc)
    except urllib.error.URLError as exc:
        detail = str(exc.reason) if isinstance(exc.reason, OSError) else str(exc)
        return {"error": ERR_GATEWAY_UNREACHABLE.format(detail=detail)}
    except (TimeoutError, socket.timeout) as exc:
        return {"error": ERR_GATEWAY_UNREACHABLE.format(detail=f"timed out ({exc})")}
    except Exception as exc:
        return {"error": ERR_GATEWAY_UNREACHABLE.format(detail=f"{type(exc).__name__}: {exc}")}
    if not isinstance(decoded, dict):
        return {"error": f"the gateway returned a malformed response for '{name}'"}
    return decoded


def run_mcp_server() -> None:
    """Run the MCP stdio server — reads JSON-RPC from stdin, writes to stdout."""
    run_mcp_stdio_loop(SERVER_NAME, SERVER_VERSION, _list_tools, _call_tool)
