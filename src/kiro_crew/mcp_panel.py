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

from kiro_crew import dashboard_agentic
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
                "crew's webview, stored for readers of GET /panel. It is NOT "
                "what the Dashboard tab draws: that tab draws the crewmate's "
                "own dashboard, so anything a person must ACT on belongs in "
                "an agentic dashboard field via dashboard_write, where it "
                "appears under 'Needs you'. Send "
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
        {
            "name": "dashboard_fields",
            "description": (
                "READ THIS BEFORE YOU WRITE. Lists every field of YOUR dashboard "
                "with its type and where its value comes from -- `fold` for a "
                "number the gateway reads out of your crew log, which you cannot "
                "write, and `agentic` for one you write yourself with "
                "dashboard_write. It also returns your own MISTAKE BOOK: the "
                "writes of yours that were refused, grouped, with how many times "
                "you made each one and the field name that worked instead. Those "
                "are mistakes you made in earlier cycles and cannot remember, so "
                "reading them is the difference between fixing a wrong field name "
                "once and rediscovering it every cycle. Takes no arguments: the "
                "dashboard it describes is your own."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "dashboard_write",
            "description": (
                "Write ONE agentic field of your dashboard -- a number, phrase or "
                "series only you know, which the page then draws. Call "
                "dashboard_fields first if you do not know your field names and "
                "types; a write naming a field your template does not declare, a "
                "field the gateway fills from a fold, or a value of the wrong "
                "type is REFUSED, and the refusal names the fields you could have "
                "used. Fix it from that list and call again; after "
                f"{dashboard_agentic.AGENTIC_RETRY_BUDGET} tries, ask the human "
                "instead of guessing further. Each write replaces that one field "
                "and leaves the others alone, so report the number you just "
                "learned rather than restating the whole dashboard. Every refusal "
                "is recorded in your mistake book, which dashboard_fields hands "
                "back -- so the same wrong guess next cycle is one you were "
                "already told about."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "maxLength": 64,
                        "description": (
                            "The agentic field to fill, exactly as dashboard_fields " "names it."
                        ),
                    },
                    "value": {
                        "description": (
                            "The value, of the type the field declares. A `number` "
                            "field takes a number and not a string holding one; a "
                            "`boolean` field takes true or false and not 1; an "
                            "`array` field takes the whole series and the page's own "
                            "script walks it."
                        ),
                    },
                },
                "required": ["field", "value"],
            },
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

    if name == "dashboard_fields":
        sk, err = _strict_session_key()
        if err:
            return err
        d = _get("/api/agent-panel/dashboard/fields", session_key=sk)
        if d.get("error"):
            return redact(f"Error: {d['error']}")
        return redact(_render_fields(d))

    if name == "dashboard_write":
        field = args.get("field")
        if not isinstance(field, str) or not field.strip():
            return "Error: `field` must name the dashboard field to fill"
        if "value" not in args:
            return "Error: `value` is required -- there is nothing to write without it"
        sk, err = _strict_session_key()
        if err:
            return err
        d = _post(
            "/api/agent-panel/dashboard/write",
            {"field": field, "value": args["value"]},
            session_key=sk,
        )
        api_err = d.get("error")
        if api_err:
            # THE REFUSAL COMES BACK WHOLE, which is the one thing this tool must
            # not shorten. It names the valid fields, the type that was wanted, and
            # how many times this mistake has been made before -- an agent handed a
            # generic failure instead would guess again, which is exactly the cycle
            # the mistake book exists to end.
            return redact(f"Error: {api_err}")
        written = d.get("written") or {}
        return redact(
            f"Wrote `{written.get('field', field)}` "
            f"({written.get('type', 'value')}) to your dashboard."
            + (" It corrected an earlier refused write." if d.get("corrected") else "")
        )

    return f"Error: unknown tool '{name}'"


def _render_fields(payload: dict[str, Any]) -> str:
    """The field list and mistake book as the agent reads them.

    PROSE rather than the raw JSON, because the reader spends context on this and
    the JSON's shape is not the message: what matters is which fields are the
    agent's to write, which are already recorded, and which of its own past guesses
    were wrong. The mistake rows lead with the count, so the one made five times is
    the one read first.
    """
    template = payload.get("template")
    lines: list[str] = []
    if not isinstance(template, dict):
        lines.append(
            "You have no dashboard yet, so there is no field to write. Ask the human "
            "to adopt a template for you."
        )
    else:
        lines.append(
            f"Dashboard: template `{template.get('id')}` version "
            f"{template.get('version')}, your copy at version "
            f"{payload.get('instance_version')}."
        )
        rows = payload.get("fields")
        if isinstance(rows, list) and rows:
            lines.append("")
            lines.append("Fields:")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("source") == "agentic":
                    lines.append(f"  {row.get('field')} ({row.get('type')}) -- YOURS to write")
                else:
                    lines.append(
                        f"  {row.get('field')} ({row.get('type')}) -- read from the "
                        f"{row.get('fold')} fold at {row.get('path')}"
                    )
    mistakes = payload.get("mistakes")
    if isinstance(mistakes, list) and mistakes:
        lines.append("")
        lines.append("Your mistake book (refused writes you have made before):")
        for row in mistakes:
            if not isinstance(row, dict):
                continue
            times = "once" if row.get("count") == 1 else f"{row.get('count')} times"
            fix = row.get("use_instead")
            tail = f" -- use `{fix}` instead" if fix else f" -- {row.get('reason')}"
            lines.append(f"  {times}: {row.get('code')} on `{row.get('field')}`{tail}")
    elif isinstance(template, dict):
        lines.append("")
        lines.append("Your mistake book is empty: no write of yours has been refused.")
    budget = payload.get("retry_budget")
    if budget:
        lines.append("")
        lines.append(
            f"A refused write names the fields you could have used. Fix it from that "
            f"list and retry; after {budget} tries, ask the human."
        )
    return "\n".join(lines)


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
