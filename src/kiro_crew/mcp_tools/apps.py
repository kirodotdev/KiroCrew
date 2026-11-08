"""The app-scoped tools reachable only through this server's credential tools: what they advertise and what they do.

``schemas()`` returns the DECLARATION half of each tool -- its name, the
model-facing description, and the JSON Schema a call is validated against.
``HANDLERS`` maps each of those names to the function that runs it. Both halves
of a tool live here so its contract and its behavior are read together, and
``test_mcp_tool_registry`` fails if one arrives without the other.

``advertised()`` is what ``tools/list`` actually emits: the declaration minus
the tools of every app that is switched off. Each tool here belongs to one
built-in app (``TOOL_APPS``) whose gateway routes refuse a call while the app is
disabled, so advertising the tool regardless tells the model about a capability
that can only refuse. The listing and the call read the SAME
``installed.json`` through the same module; ``advertised()`` states the one
state in which the two readers differ.

Handlers reach this server's shared plumbing as attributes of ``mcp_core`` --
``mcp_core._post``, the identity resolvers, the governance vets. That is
deliberate rather than untidy: an attribute lookup resolves at CALL time, so a
test that rebinds one on the module still intercepts the handler. Importing
those names directly here would bind them at import time and silently escape
every existing patch site.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.apps.manager import app_enabled_state
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import OPS_MISSION_CONTROL_ALLOWED_CALLS, sanitize_json_values


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the apps tools."""
    return [
        {
            "name": "ops_mission_control_api",
            "description": (
                "Call the Ops Mission Control app's HTTP API with the gateway's "
                "own credential. This is the ONLY way an agent session reaches "
                "that API: raw HTTP has no credential and is refused with 403, "
                "and no CLI or environment variable provides one. Use it for "
                "every call an ops-mission-control SOP asks for — reading "
                "state/signals/incidents/rotation/ledger, claiming and "
                "transitioning incidents, arming the rotation, posting ledger "
                "entries. Paths are rooted at the app base (pass '/state', not "
                "the full URL) and only the SOP surface is reachable: "
                "provider configuration, settings, webhook ingest and the "
                "human proposal-decision routes are deliberately not callable "
                "from here."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST"],
                        "description": "HTTP method",
                    },
                    "path": {
                        "type": "string",
                        # Derived from the frozen allowlist so the advertised
                        # schema cannot drift from what the handler admits.
                        "enum": sorted({p for _, p in OPS_MISSION_CONTROL_ALLOWED_CALLS}),
                        "description": (
                            "API path relative to /api/apps/ops-mission-control. "
                            "GET: /state /signals /incidents /handover /rotation "
                            "/ledger /ledger/contradictions. POST: /dispatch "
                            "/incident/transition /incident/claim /incident/action "
                            "/rotation/arm /ledger /ledger/hygiene."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional query string without the leading '?', GET only "
                            "— e.g. 'status=investigating' or 'id=INV-42' on /incidents"
                        ),
                    },
                    "body_json": {
                        "type": "string",
                        "description": (
                            "JSON object for POST bodies, serialized as a string — "
                            'e.g. \'{"id": "INV-42", "status": "resolved"}\' for '
                            "/incident/transition"
                        ),
                    },
                },
                "required": ["method", "path"],
            },
        },
    ]


#: Which built-in app each tool reaches, by the app's installed name (the directory
#: under ``apps/`` whose ``installed.json`` the app's gateway routes gate on). Spelled
#: as literals rather than imported from the apps: each app's ``APP_NAME`` lives in
#: a backend module that drags that app's whole import graph into this server
#: process. ``test_mcp_apps_tool_gate`` pins every entry to the real constant. A tool
#: an installed app declares in its manifest is the ``app_tools`` domain's.
TOOL_APPS: dict[str, str] = {
    "ops_mission_control_api": "ops-mission-control",
}


def advertised() -> list[dict[str, Any]]:
    """The descriptors ``tools/list`` emits now: ``schemas()`` minus disabled apps' tools.

    The listing and the call read ONE file through ONE module -- the app's
    ``installed.json`` via ``kiro_crew.apps.manager`` -- with two readers. Every
    route these tools reach refuses the call with HTTP 403 while the binary
    ``is_app_enabled`` does not prove the app enabled (the app reverse proxy a
    declared tool posts through names the refusal ``app_not_enabled``, Ops Mission
    Control ``app_disabled``). This reads the tri-state ``app_enabled_state`` and hides only on a
    readable ``False`` -- not installed, or installed and switched off. So a
    hidden tool is always one whose call would be refused, while a listed tool is
    not always one whose call would succeed: an unreadable record lists tools the
    route refuses. Hiding is a product decision, not a security one -- the route
    stays the enforcement point, as it does for the computer-use tools -- and it
    exists because kiro-cli caches ``tools/list`` once per session, so a disabled
    app's tools would otherwise spend context in every request of every session
    that can never use them.

    Fail-OPEN on a read fault, as the per-session policy filter does: ``None``
    (corrupt JSON, a dangling link, a directory in the file's place) lists the
    tools, and so does a read that raises. The same once-per-session cache that
    motivates hiding makes hiding on a transient fault permanent for the
    session's whole life, while a listed tool whose call the gateway refuses is
    not a hole; an exception escaping this build, by contrast, would withdraw
    EVERY tool of the pooled server for every session that shares it.

    Only the FULL descriptor build reaches this function. The names-only build
    (``mcp_tools.build_tool_names``, which in-process discovery reads on the
    gateway's event loop) takes ``schemas()`` directly -- the same rule that
    keeps ``spawn.schemas`` and ``control.schemas`` from their live reads on that
    path -- so no ``installed.json`` is read there and a disabled app's tools
    still appear by name. The process that serves ``tools/list`` to a model is
    ``mcp_shared.run_mcp_stdio_loop``, which takes the full build, where the gate
    IS applied.
    """
    hidden: set[str] = set()
    for app in sorted(set(TOOL_APPS.values())):
        try:
            state = app_enabled_state(app)
        except Exception:
            # Never let a metadata read break the tool advertisement: the route
            # still refuses the call, while an escaping exception withdraws every tool.
            continue
        if state is False:
            hidden.update(tool for tool, owner in TOOL_APPS.items() if owner == app)
    return [spec for spec in schemas() if spec["name"] not in hidden]


def ops_mission_control_api(name: str, args: dict[str, Any]) -> str:
    _omc_method = args["method"]
    _omc_path = args["path"]
    if (_omc_method, _omc_path) not in OPS_MISSION_CONTROL_ALLOWED_CALLS:
        return (
            f"Error: {_omc_method} {_omc_path} is not part of the "
            "ops-mission-control agent surface."
        )
    _omc_body: dict[str, Any] = {}
    _omc_body_raw = args.get("body_json") or ""
    if _omc_body_raw:
        try:
            _omc_parsed = json.loads(_omc_body_raw)
        except ValueError:
            return "Error: body_json is not valid JSON."
        if not isinstance(_omc_parsed, dict):
            return "Error: body_json must encode a JSON object."
        # Schema sanitization saw body_json as one opaque string, where a
        # ``\u200b`` escape is plain ASCII — the hidden character only
        # materializes on decode. Walk the decoded structure so smuggled
        # invisibles cannot split a credential past downstream redaction.
        # Then redact on the way IN as well: ledger entries persist and
        # sync to a configured remote, so a credential quoted into a body
        # field must be scrubbed BEFORE ``_post``, not only on the
        # response path.
        _omc_body = mcp_core._redact_json_strings(sanitize_json_values(_omc_parsed))
    _omc_query = args.get("query") or ""
    _omc_url = "/api/apps/ops-mission-control" + _omc_path
    if _omc_query:
        _omc_url += "?" + _omc_query
    _omc_resp = (
        mcp_core._get(_omc_url) if _omc_method == "GET" else mcp_core._post(_omc_url, _omc_body)
    )
    # Serialize compactly and redact on the way OUT: signals, incident
    # titles and ledger entries carry text from external monitoring
    # systems and prior LLM turns, so a credential or exfil URL quoted
    # into one would otherwise flow straight into this agent's context.
    # Redact BEFORE truncating: slicing first could cut a credential in
    # half at the cap so the redaction pattern would not match, leaking
    # the surviving fragment.
    _omc_text = redact(json.dumps(_omc_resp, ensure_ascii=False, default=str))
    _omc_cap = 60_000
    if len(_omc_text) > _omc_cap:
        _omc_text = (
            _omc_text[:_omc_cap] + f"\n… truncated ({len(_omc_text)} chars total). Narrow the "
            "call (e.g. query filters) to see the rest."
        )
    return _omc_text


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "ops_mission_control_api": ops_mission_control_api,
}
