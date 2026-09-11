"""MCP server ``kirocrew-panel`` — a crew fills in its own webview.

Every crew gets a webview in its drawer, and this server is the one write path
into it. A long-running crew knows things nobody can read: how many workers it
holds, which one is stuck, what it will do next, whether anything is waiting on
a decision.

Deliberately narrow: the crew publishes a DATA OBJECT and names a TEMPLATE to
render it with. It never sends markup, because the template is the human-authored
half.

Why this is its own server, not a tool on an existing one
--------------------------------------------------------
Assignment is per server, so the server IS the unit of authorization. The
dashboard server's set is ratcheted to folder organization plus session control
and a document-publishing tool belongs to neither class; putting it there would
widen a set the user granted for something else. So it gets a server, marked
``opt_in`` in ``agent._MANAGED_MCP_SERVERS``, which means a default agent's
spec carries neither the entry nor an ``@kirocrew-panel`` reference and spends
no context on it. Only an agent whose own spec names the set can reach it.

Why the tool has no session argument
------------------------------------
The panel a call writes is derived from the CALLING SESSION's identity,
resolved strictly, and passed explicitly to the transport so the value that was
checked is the value that is used. The lenient resolver walks ``/proc``
ancestors, and a subagent lives inside its parent slot's process tree — that
walk would let a subagent overwrite its parent's panel. A subagent has no panel
of its own and is told so.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.mcp_core import (
    _get,
    _post,
    _resolve_session_key,
    require_strict_session_key,
)
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import MCP_PANEL_SCHEMAS, validate_tool_args

logger = logging.getLogger(__name__)

SERVER_NAME = "kirocrew-panel"
SERVER_VERSION = "1.0.0"


def _tool_definitions() -> list[dict[str, Any]]:
    """The tool surface: publish a panel, and discover what can render it."""
    return [
        {
            "name": "panel_publish",
            "description": (
                "Publish what the human watching you should see into YOUR "
                "crew's webview, shown in your drawer on the Crew page. Send "
                "DATA, not layout: you "
                "pass a JSON object and name a template that renders it, so "
                "the panel keeps a stable shape across cycles and costs you a "
                "few hundred bytes instead of a screenful of markup. Call it "
                "once per cycle of long-running work, after you have decided "
                "what changed — a panel answers 'what is this agent holding, "
                "what is stuck, what needs me', so lead with the thing that "
                "needs a human and keep counters secondary. Each call REPLACES "
                "the whole panel: include everything still true, not just the "
                "delta. Use panel_templates first if you do not know which "
                "template ids exist; the `default` template renders any object "
                "without being told what the fields mean."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "data": {
                        "type": "object",
                        "description": (
                            "The state to render. Shape drives presentation in "
                            "the default template: a scalar becomes a stat "
                            "tile, an array of objects becomes a table, an "
                            "array of scalars a list, a nested object a "
                            "key/value block. Field names are shown to the "
                            "user, so name them for a reader "
                            "('waiting_on_you', not 'wf3')."
                        ),
                    },
                    "template": {
                        "type": "string",
                        "maxLength": 64,
                        "description": (
                            "Template id to render with. Defaults to "
                            "`default`, which handles any object. A bespoke "
                            "template exists for some agents — panel_templates "
                            "lists what is installed."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "maxLength": 200,
                        "description": (
                            "Short name for this panel, shown in the page's "
                            "picker (e.g. 'fleet — cycle 47')."
                        ),
                    },
                },
                "required": ["data"],
            },
        },
        {
            "name": "panel_templates",
            "description": (
                "List the template ids panel_publish can render with, including "
                "any the operator installed themselves, and report which one "
                "your crew gets by default. Call it when you want a template "
                "other than your crew's own."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _list_tools() -> list[dict[str, Any]]:
    """The tool surface, unconditionally.

    Reaching this process at all means an agent spec referenced the set, so the
    assignment already happened and there is nothing left to gate here.
    """
    return _tool_definitions()


def _strict_session_key() -> tuple[str, str]:
    """Resolve the calling session strictly. Returns ``(key, "")`` or ``("", err)``.

    Strict because the lenient resolver's ``/proc`` ancestor walk resolves a
    subagent to its PARENT slot, which would let a subagent overwrite the
    parent's panel.

    Routed through ``mcp_core.require_strict_session_key`` rather than calling the
    raw resolver: that helper is the ONE fail-closed identity gate every reflexive
    tool shares, and a ratchet over ``mcp_core.REFLEXIVE_TOOL_MODULES`` exists to
    stop the next reflexive tool reaching for the lenient resolver instead. The
    gate appends ``strict_identity_diagnosis`` itself, so the refusal below is the
    caller-facing half only.
    """
    return require_strict_session_key(
        "Error: this session's identity could not be verified strictly, so "
        "there is no panel to publish to from here. Subagents inherit no "
        "session identity of their own — publish from the parent session "
        "instead.",
        SERVER_NAME,
    )


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    schema = MCP_PANEL_SCHEMAS.get(name)
    if schema is None:
        return args
    return validate_tool_args(args, schema)


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    """Dispatch one validated tool call."""
    if name == "panel_templates":
        sk, err = _strict_session_key()
        if err:
            return err
        d = _get("/api/agent-panel/templates", session_key=sk)
        if d.get("error"):
            return redact(f"Error: {d['error']}")
        ids = d.get("templates") or []
        if not ids:
            return "No panel templates are installed."
        default = d.get("default") or "default"
        return (
            f"Your crew's webview renders with `{default}` unless you name "
            "another. Installed: " + ", ".join(str(i) for i in ids)
        )

    if name == "panel_publish":
        data = args.get("data")
        if not isinstance(data, dict):
            return "Error: `data` must be a JSON object describing what to show"
        payload: dict[str, Any] = {"data": data}
        for key in ("template", "title"):
            value = args.get(key)
            if value is not None:
                payload[key] = value
        sk, err = _strict_session_key()
        if err:
            return err
        d = _post("/api/agent-panel/publish", payload, session_key=sk)
        api_err = d.get("error")
        if api_err:
            # The refusal codes are actionable by the caller (a bad template id,
            # data over the cap), so the prose comes back rather than a generic
            # failure the agent cannot correct on its next cycle.
            return redact(f"Error: {api_err}")
        published = d.get("panel") or {}
        template = published.get("template") or "default"
        fields = len(data)
        return (
            f"Published to your crew's webview using the `{template}` template "
            f"({fields} top-level field{'' if fields == 1 else 's'}). "
            "It replaced the previous panel."
        )

    return f"Error: unknown tool '{name}'"


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    """Guarded entry point — schema validation and SEL audit live in the wrapper."""
    return call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        session_key=_resolve_session_key() or SERVER_NAME,
        downstream_service=SERVER_NAME,
    )


#: This server consumes the per-call caller block the gateway injects rather
#: than reading identity from its own process, and refuses a caller the gateway
#: cannot name — so it is safe in the shareable set. Kept in step with
#: ``mcp_discovery._MANAGED_SERVERS_CALLER_AWARE`` by a ratchet test.
ADVERTISE_CALLER_IDENTITY = True


def run_mcp_server() -> None:
    """Run the MCP stdio server — reads JSON-RPC from stdin, writes to stdout."""
    run_mcp_stdio_loop(
        SERVER_NAME,
        SERVER_VERSION,
        _list_tools,
        _call_tool,
        advertise_caller_identity=ADVERTISE_CALLER_IDENTITY,
    )


if __name__ == "__main__":  # pragma: no cover - process entry
    logging.basicConfig(level=logging.INFO)
    run_mcp_server()
