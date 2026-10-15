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

import json
import logging
from typing import Any
from urllib.parse import quote

from kiro_crew import dashboard_agentic
from kiro_crew.dashboard_templates.instance import AUTHORED_PAGE_REFUSAL
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
                "Publish what the human watching you should see for YOUR "
                "crew, stored for readers of GET /panel. The Dashboard tab "
                "shows it only if its page binds the published board; "
                "dashboard_fields says whether yours does. Send "
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
                            "Short name for this panel (e.g. 'fleet — cycle "
                            "47'), used as the panel's heading when data carries "
                            "no title of its own."
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
                "dashboard_write. An agentic field shows its full shape (the keys "
                "each row must carry, the allowed values) and its current value; a "
                "fold field shows one summary line. It also returns your own MISTAKE BOOK: the "
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
            "name": "dashboard_types",
            "description": (
                "READ THIS BEFORE YOU COMPOSE A DASHBOARD. Lists every DATA TYPE "
                "you can bind a dashboard block to -- one per fold of your crew "
                "log -- and for each one: its name, the field shape inside it (so "
                "you can name a path instead of guessing one), which fold the "
                "value is folded from, and how far that fold has been folded for "
                "you right now. A block bound to a path this does not list draws "
                "nothing, so read the shape and copy the path out of it. Three "
                "things to read carefully: an `array` is where the shape STOPS, "
                "because a path cannot reach inside a list; an `opaque` object is "
                "keyed by an id nobody can list ahead of time (a model name, a "
                "subagent id), so a path below it may well work and this tool "
                "cannot promise it; and `folded_through: null` means that fold's "
                "position was NOT read, which is different from 0, meaning nothing "
                "has been folded yet. It is a pure read: nothing is written, "
                "staged or changed. Takes no arguments."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "dashboard_write",
            "description": (
                "Write ONE agentic field of your dashboard -- a number, phrase or "
                "series only you know, which the page then draws. Call "
                "dashboard_fields first if you do not know your field names and "
                "shapes; a write naming a field your template does not declare, a "
                "field the gateway fills from a fold, or a value of the wrong "
                "type or shape (an unknown key, a missing required key, a value "
                "outside an allowed list) is REFUSED, and the refusal names the "
                "fields or keys you could have used. Fix it from that list and call again; after "
                f"{dashboard_agentic.AGENTIC_RETRY_BUDGET} tries, ask the human "
                "instead of guessing further. Each write replaces that one field "
                "and leaves the others alone, so report the number you just "
                "learned rather than restating the whole dashboard. Every refusal "
                "is recorded in your mistake book, which dashboard_fields hands "
                "back -- so the same wrong guess next cycle is one you were "
                "already told about. To EMPTY an array or object field, pass "
                "`clear: true` and no `value`: an MCP client can drop `value: []` "
                "or `value: {}` from the call before it reaches this tool, while a "
                "boolean arrives intact."
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
                            "script walks it. Required unless `clear` is true."
                        ),
                    },
                    "clear": {
                        "type": "boolean",
                        "description": (
                            "true empties the field: an `array` field gets `[]` and "
                            "an `object` field gets `{}`. Use it instead of "
                            "`value: []` or `value: {}`, and leave `value` out."
                        ),
                    },
                },
                "required": ["field"],
            },
        },
        {
            "name": "dashboard_save",
            "description": (
                "SAVE the page you composed and PUT IT LIVE as YOUR crewmate's "
                "Dashboard. A page is three declarations and this call saves all "
                "three at once: a "
                "`model` naming the fields the page holds and where each value "
                "comes from, a `view` laying out the blocks that draw them, and a "
                "`theme` of tokens. Read dashboard_fields first -- it tells you "
                "the page in force -- and tell the person what you are changing, "
                "because the page is theirs. A field reads either from a crew-log fold "
                '(`{"fold": <name>, "path": <dotted keys>}`, filled by the '
                'gateway) or from you (`{"agentic": true}`) -- never a number you '
                "type into the layout, which would be true only at the moment you "
                "typed it. The "
                "block types and data types come from a CLOSED catalog, so a "
                "package naming one that does not exist is refused and the refusal "
                "lists the ones that do: recompose from that list. NAME YOUR FIELDS "
                "AND BLOCKS FOR A READER -- `open_prs`, not a token. A name a "
                "redactor would mask is refused, because a name is a key the page "
                "joins its cells by and cannot be rewritten on the way out; the "
                "refusal tells you which position to rename. Values never version "
                "and never belong in a package. This call STORES the page as your "
                "own dashboard package; dashboard_apply with no arguments then puts "
                "it live as a new version -- no preview step. Once live, "
                "dashboard_write keeps working on its agentic fields and "
                "dashboard_fields lists them. To go back, dashboard_rollback to the "
                "version you were on -- a template version brings the template "
                "page back."
                " A page you write as `view.html` is RUN once by the gateway over a "
                "SYNTHETIC copy of your crewmate's current data (the same shape with "
                "invented values, so a page never sees the record; a key is kept only if "
                "your page spells it out or the dashboard type catalog lists it), and the "
                "reply carries a `verification` block: `passed`, `failed` with each script "
                "error and the `view.html` line and column it was thrown at, `timed_out` "
                "when the run hit its time limit, or `skipped` with the reason. Fix a "
                "failed page and save again BEFORE you dashboard_apply it. The run is "
                "offline: a library the page loads from the CDN allowlist is not fetched, "
                "and a page whose script stops at that library's name is `skipped`, not "
                "`failed`. The page is checked as the dashboard shows it: the gateway's "
                "secret filter rewrites text in a page that looks like a key (a long run "
                "of letters and digits can), and when it did the reply says how many "
                "places and which line the first is on. One page is checked at a time "
                "across the gateway, so a save that finds it busy, or a machine short of "
                "memory, is `skipped` and does not wait. The save waits for the run, "
                "usually about a second and never more than about twelve."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "model": {
                        "type": "object",
                        "description": (
                            "The fields the page holds, as "
                            '`{"types": {<field_name>: {...}}}`. Each field names '
                            "a data type (`number`, `text`, `bool`, `timestamp`, "
                            "`enum`) and a `source`, and may carry a `label` and "
                            "`description` a reader sees. Field names are "
                            "lowercase letters, digits and underscores."
                        ),
                    },
                    "view": {
                        "type": "object",
                        "description": (
                            'The layout, as `{"blocks": [...]}`, in the order a '
                            "reader meets them -- that order IS the layout. Each "
                            "block carries an `id`, a `type` from the block "
                            "catalog, the `fields` it draws (every one declared by "
                            "the model) and an optional `title`."
                        ),
                    },
                    "theme": {
                        "type": "object",
                        "description": (
                            'Look, as `{"tokens": {"--name": "value"}}`, with an '
                            'optional `"css"` string beside it for rules the '
                            "tokens cannot express. None of "
                            "the look is fixed by the product, so this is where "
                            "you set it. An empty `tokens` object is valid and "
                            "takes the defaults."
                        ),
                    },
                },
                "required": ["model", "view", "theme"],
            },
        },
        {
            "name": "dashboard_templates",
            "description": (
                "List or SEARCH the dashboard pages this gateway can give your "
                "crewmate, and say which one it is on now. Pass `query` to "
                "search; it matches a template's name and description AND the "
                "fold paths its fields read, so asking for `cost` finds the page "
                "that shows a usage number whether or not its author used that "
                "word. Omit `query` for all of them. This is the FIRST step of "
                "'show me another one': list, then dashboard_preview the one that "
                "fits, then ask the person before you apply it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "maxLength": 200,
                        "description": (
                            "What the page should show, in the person's own words. "
                            "Every word must match somewhere in the template."
                        ),
                    },
                },
            },
        },
        {
            "name": "dashboard_preview",
            "description": (
                "Stage one of this gateway's templates for the person to LOOK at. "
                "NOTHING IS RECORDED: their current page is untouched, no version "
                "is written, and the answer carries a link they open to see it. "
                "Name a `template_id` from dashboard_templates -- that is the only "
                "argument. A preview is OPTIONAL, never a step before applying: use "
                "it only when the person asks to look first. dashboard_apply with "
                "the `template_id` puts it live. A page you composed yourself is "
                "saved with dashboard_save, not previewed here. While the person has "
                "locked the layout (your [DASHBOARD] block says LOCKED), you can "
                "still preview a page for them to look at, but do not apply it: "
                "ask them to unlock it first."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "template_id": {
                        "type": "string",
                        "maxLength": 64,
                        "description": "A template id dashboard_templates listed.",
                    },
                },
                # NOT declared required, even though it is the only argument and an
                # absent one is refused. A schema-level requirement is reported as a
                # missing field, and the call this surface most needs to explain is the
                # one that sent a `manifest` and `html` instead -- that caller has to
                # read why its page cannot render, not which key it left out.
            },
        },
        {
            "name": "dashboard_apply",
            "description": (
                "Put a page live on your crewmate's Dashboard, now, as a new "
                "version -- there is no preview step. Pass a `template_id` from "
                "dashboard_templates to use a shipped or operator page, or pass "
                "nothing to put live the page you saved with dashboard_save. Every "
                "earlier version stays on disk, so dashboard_rollback goes back in "
                "one call. Tell the person what you changed. Refused with "
                "`dashboard_locked` while the page is locked (your [DASHBOARD] "
                "block says LOCKED)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "template_id": {
                        "type": "string",
                        "maxLength": 64,
                        "description": (
                            "A template id dashboard_templates listed. Omit it to "
                            "put live the page you saved with dashboard_save."
                        ),
                    },
                },
            },
        },
        {
            "name": "dashboard_rollback",
            "description": (
                "Go BACK to an earlier version of your crewmate's page -- the "
                "answer to the person saying 'go back'. A version can be a template "
                "page or a page you saved with dashboard_save, and the one you "
                "restore is the one that draws. dashboard_fields lists the "
                "versions you can still reach; a version the store has dropped is "
                "refused, so read that list rather than guessing. A rollback moves "
                "FORWARD: restoring version 1 over version 2 writes version 3, so "
                "nothing is lost and you can roll back again. Refused with "
                "`dashboard_locked` while the page is locked (your [DASHBOARD] "
                "block says LOCKED)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to_version": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "The version to restore, from dashboard_fields' list of "
                            "the ones a rollback can reach."
                        ),
                    },
                },
                "required": ["to_version"],
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
            f"Published using the `{template}` template "
            f"({fields} top-level field{'' if fields == 1 else 's'}). "
            "It replaced the previous panel. The Dashboard tab shows it only "
            "if its page binds the published board; dashboard_fields says "
            "whether yours does."
        )

    if name == "dashboard_fields":
        sk, err = _strict_session_key()
        if err:
            return err
        d = _get("/api/agent-panel/dashboard/fields", session_key=sk)
        if d.get("error"):
            return redact(f"Error: {d['error']}")
        return redact(_render_fields(d))

    if name == "dashboard_types":
        sk, err = _strict_session_key()
        if err:
            return err
        d = _get("/api/agent-panel/dashboard/types", session_key=sk)
        if d.get("error"):
            return redact(f"Error: {d['error']}")
        return redact(_render_types(d))

    if name == "dashboard_write":
        field = args.get("field")
        if not isinstance(field, str) or not field.strip():
            return "Error: `field` must name the dashboard field to fill"
        to_write: Any
        clear = args.get("clear") is True
        if not clear and "value" not in args:
            return "Error: `value` is required -- there is nothing to write without it"
        sk, err = _strict_session_key()
        if err:
            return err
        if clear:
            # The empty value is spelled HERE, not by the caller: an MCP client that
            # drops an empty container from a call's arguments delivers a boolean
            # intact. Which empty value depends on the field's declared type, read
            # from the same list dashboard_fields shows; the gateway still
            # type-checks the write like any other.
            to_write = _empty_value_for(field, sk)
        else:
            to_write = args["value"]
        d = _post(
            "/api/agent-panel/dashboard/write",
            {"field": field, "value": to_write},
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

    if name == "dashboard_save":
        # THE THREE, named one by one rather than forwarded as ``args``. The
        # gateway fills the package's other two keys -- ``kind`` and the
        # ``bound_to`` that decides whose tab renders this page -- from the caller
        # it vetted, so building the payload from an explicit list is what makes a
        # fourth key impossible to pass through from here. The gateway refuses one
        # too; this is the half that cannot send it.
        page: dict[str, Any] = {}
        for key in ("model", "view", "theme"):
            part = args.get(key)
            if not isinstance(part, dict):
                return (
                    f"Error: `{key}` must be an object -- a page is a `model`, a "
                    "`view` and a `theme`, and all three are saved together"
                )
            page[key] = part
        sk, err = _strict_session_key()
        if err:
            return err
        d = _post("/api/agent-panel/dashboard/save", page, session_key=sk)
        api_err = d.get("error")
        if api_err:
            # WHOLE, like a refused write. A package refusal names the key path,
            # what was wrong with it, and for a closed catalog the values that
            # would have been taken -- which is the list the next compose is built
            # from. Shortened to a generic failure it would cost a cycle and teach
            # nothing.
            return redact(f"Error: {api_err}")
        return redact(_render_saved(d.get("saved") or {}, d.get("verification")))

    if name == "dashboard_templates":
        query = args.get("query")
        if query is not None and not isinstance(query, str):
            return "Error: `query` must be a string"
        sk, err = _strict_session_key()
        if err:
            return err
        path = "/api/agent-panel/dashboard/templates"
        if isinstance(query, str) and query.strip():
            path = f"{path}?query={quote(query.strip(), safe='')}"
        d = _get(path, session_key=sk)
        if d.get("error"):
            return redact(f"Error: {d['error']}")
        return redact(_render_templates(d))

    if name == "dashboard_preview":
        template_id = args.get("template_id")
        if args.get("manifest") is not None or args.get("html") is not None:
            # Answered HERE as well as by the store, so an agent that wrote a page gets
            # the reason without a round trip -- and gets the SAME reason either way.
            return f"Error: {AUTHORED_PAGE_REFUSAL}"
        if not isinstance(template_id, str) or not template_id.strip():
            return "Error: nothing to preview -- pass a `template_id` from " "dashboard_templates"
        sk, err = _strict_session_key()
        if err:
            return err
        staging: dict[str, Any] = {"template_id": template_id}
        d = _post("/api/agent-panel/dashboard/preview", staging, session_key=sk)
        api_err = d.get("error")
        if api_err:
            # WHOLE, like a refused write: a parity refusal names every field the page
            # binds that the manifest does not and the other way round, which is the
            # list an agent fixes the page from. A generic failure would cost a cycle.
            return redact(f"Error: {api_err}")
        return redact(_render_preview(d.get("preview") or {}))

    if name == "dashboard_apply":
        template_id = args.get("template_id")
        if template_id is not None and (
            not isinstance(template_id, str) or not template_id.strip()
        ):
            return "Error: `template_id` must be a template id from dashboard_templates"
        sk, err = _strict_session_key()
        if err:
            return err
        applying: dict[str, Any] = {} if template_id is None else {"template_id": template_id}
        d = _post("/api/agent-panel/dashboard/apply", applying, session_key=sk)
        api_err = d.get("error")
        if api_err:
            return redact(f"Error: {api_err}")
        shown = (
            "the page you saved with dashboard_save"
            if d.get("package")
            else f"template `{d.get('template_id')}`"
        )
        return redact(
            f"LIVE: your crewmate's page is now version {d.get('instance_version')}, "
            f"{shown}. dashboard_rollback goes back to any earlier version." + _guide_tail(d)
        )

    if name == "dashboard_rollback":
        to_version = args.get("to_version")
        if not isinstance(to_version, int) or isinstance(to_version, bool) or to_version < 1:
            return (
                "Error: `to_version` must be a positive integer -- dashboard_fields "
                "lists the versions a rollback can still reach"
            )
        sk, err = _strict_session_key()
        if err:
            return err
        d = _post("/api/agent-panel/dashboard/rollback", {"to_version": to_version}, session_key=sk)
        api_err = d.get("error")
        if api_err:
            # The refusal names the versions still kept, which is what an agent that
            # guessed needs in order not to guess again.
            return redact(f"Error: {api_err}")
        return redact(
            f"Restored version {d.get('restored_from')} as version "
            f"{d.get('instance_version')} -- a rollback moves forward, so the page you "
            "were on is still there to go back to." + _guide_tail(d)
        )

    return f"Error: unknown tool '{name}'"


def _guide_tail(d: dict[str, Any]) -> str:
    """The new page's ``[DASHBOARD]`` block and upkeep pointer, after a blank line.

    The prompt carries them only from the next session start or compaction, so the
    reply that changed the page hands over the new ones now.
    """
    block = d.get("dashboard")
    return f"\n\n{block}" if isinstance(block, str) and block else ""


_ERROR_LABELS = {
    "rejection": "unhandled promise rejection: ",
    "csp": "refused by the frame's policy: ",
}


#: What the agent is told the page ran over, when it ran over anything.
_SYNTHETIC = (
    "a synthetic copy of your crewmate's current data (the same shape with invented values)"
)


def _error_lines(verification: dict[str, Any]) -> list[str]:
    """The numbered errors of a ``failed`` or ``timed_out`` block, each with its position."""
    errors = [e for e in verification.get("errors") or [] if isinstance(e, dict)]
    lines = []
    for number, error in enumerate(errors, start=1):
        where = ""
        if error.get("line") is not None:
            where = f" (view.html line {error['line']}"
            if error.get("column") is not None:
                where += f", column {error['column']}"
            where += ")"
        label = _ERROR_LABELS.get(str(error.get("kind")), "")
        lines.append(f"  {number}. {label}{error.get('message')}{where}")
    hidden = int(verification.get("omitted") or 0)
    if hidden:
        lines.append(f"  ... and {hidden} more.")
    return lines


def _render_verification(verification: Any) -> list[str]:
    """The ``verification`` block of a save reply, as lines for the agent.

    Leads with the verdict, because the verdict decides what the agent does next: a
    ``failed`` page is fixed and saved again before anyone is told about it. Each error
    carries where it was thrown, as a position in the agent's own ``view.html``, which is
    the one thing it cannot find out from its own session. A page that ran says what it
    ran over: a copy with the record's shape and invented values.
    """
    if not isinstance(verification, dict):
        return []
    status = verification.get("status")
    ran_over = _SYNTHETIC if verification.get("data") == "synthetic" else "your crewmate's data"
    if status == "passed":
        lines = [
            f"verification: passed -- the page ran once over {ran_over} and raised no script errors."
        ]
    elif status == "skipped":
        lines = [
            f"verification: skipped -- {verification.get('reason') or 'the check did not run'}. "
            "The page was not verified: a script error in it may still be there."
        ]
    elif status == "timed_out":
        errors = _error_lines(verification)
        lines = [
            f"verification: timed_out -- {verification.get('reason') or 'the check ran out of time'}. "
            "The page was not verified. If a script of yours has a loop or a wait that may never "
            "end, fix it and call dashboard_save again; otherwise call it again."
        ]
        lines.extend(errors)
    elif status == "failed":
        errors = _error_lines(verification)
        count = len(verification.get("errors") or []) + int(verification.get("omitted") or 0)
        lines = [
            f"verification: failed -- the page ran once over {ran_over} and raised {count} "
            "error(s). Fix them and call dashboard_save again before you describe the page "
            "to the person."
        ]
        lines.extend(errors)
    else:
        return []
    lines.extend(f"  note: {note}" for note in verification.get("notes") or [])
    return lines


def _render_saved(saved: dict[str, Any], verification: Any = None) -> str:
    """What was SAVED, the page check's verdict, and the one call that puts it live.

    The verdict comes before the next step: a ``failed`` page is fixed and saved again
    before it is applied.
    """
    lines = [
        f"SAVED as your dashboard package ({saved.get('fields')} field(s) in "
        f"{saved.get('blocks')} block(s)). It is not live yet."
    ]
    checked = _render_verification(verification)
    if checked:
        lines.append("")
        lines.extend(checked)
    lines.append("")
    lines.append(
        "Next: call dashboard_apply with no arguments to put it on your crewmate's "
        "Dashboard as a new version."
    )
    return "\n".join(lines)


def _render_templates(payload: dict[str, Any]) -> str:
    """The catalog as the agent reads it. Prose, like the field list.

    Each row leads with the id, because the id is what dashboard_preview takes, and
    carries the title and description, because those are what a person recognises. The
    fields are listed last and the PAGE is never carried: an agent choosing between
    templates does not read markup, and the catalog's own listing leaves it out for the
    same reason.
    """
    rows = payload.get("templates")
    query = str(payload.get("query") or "")
    if not isinstance(rows, list) or not rows:
        if query:
            return (
                f"No dashboard template matches {query!r}. Every word has to match, so "
                "try fewer or more ordinary ones, or call this again with no `query` to "
                "see all of them."
            )
        return (
            "This gateway has no dashboard template to offer. Tell the person: there is "
            "nothing here to adopt, and a page you write yourself cannot be rendered."
        )
    current = str(payload.get("current_template_id") or "")
    lines: list[str] = []
    lines.append(
        f"{len(rows)} dashboard template{'' if len(rows) == 1 else 's'}"
        + (f" matching {query!r}" if query else "")
        + (f"; your crewmate is on `{current}`." if current else ".")
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        mark = " (ON NOW)" if row.get("current") else ""
        lines.append("")
        lines.append(f"  `{row.get('id')}` v{row.get('version')}{mark} -- {row.get('title')}")
        lines.append(f"    {row.get('description')}")
        fields = row.get("fields")
        if isinstance(fields, list) and fields:
            lines.append(f"    shows: {', '.join(str(f) for f in fields)}")
    problems = payload.get("problems")
    if isinstance(problems, list) and problems:
        lines.append("")
        # REPORTED, not hidden. A template somebody saved that will not load is
        # invisible otherwise, and the person who saved it is the only one who can fix
        # it -- so the agent they are talking to has to be able to tell them.
        lines.append("Templates that would not load (tell the human):")
        for row in problems:
            if isinstance(row, dict):
                lines.append(f"  {row.get('template')}: {row.get('problem')}")
    lines.append("")
    lines.append(
        "Next: dashboard_apply with the template_id that fits. dashboard_preview it "
        "first only if the person wants to look before choosing."
    )
    return "\n".join(lines)


#: How many paths one type's shape is listed with. A composing agent needs the paths it
#: can bind, and a fold wide enough to pass this is one whose remaining paths are
#: siblings of ones already shown -- so the omission is REPORTED with the count rather
#: than silently trimmed, and the whole shape is still in the JSON this prose came from.
_MAX_PATHS_PER_TYPE = 40


def _flatten(node: Any, prefix: str = "") -> list[str]:
    """One ``"<path>: <type>"`` line per bindable path inside a shape node.

    PATHS, not nested JSON, because a path is what the agent copies into a Model field
    and a nested object costs it three lines to read one. The walk stops at an array
    (nothing below is addressable) and at an opaque object (nothing below is this
    catalog's to name), and says which of the two it was.
    """
    if not isinstance(node, dict):
        return []
    ntype = str(node.get("type", "?"))
    label = prefix or "(the whole value)"
    if node.get("opaque") is True:
        keyed = node.get("keyed_by")
        return [f"{label}: object, keyed by {keyed}" if keyed else f"{label}: object, keyed by ids"]
    properties = node.get("properties")
    if ntype == "object" and isinstance(properties, dict) and properties:
        out: list[str] = []
        for key, sub in properties.items():
            out.extend(_flatten(sub, f"{prefix}.{key}" if prefix else str(key)))
        return out
    tail = " (may be null)" if node.get("nullable") else ""
    if ntype == "array":
        tail += " -- a path cannot reach inside it"
    return [f"{label}: {ntype}{tail}"]


def _render_types(payload: dict[str, Any]) -> str:
    """The data-type catalog as the agent reads it: one type, then its paths.

    The four facts the composing agent needs lead each type -- what it is called, which
    fold it is folded from, what keys it, and how far it has been folded -- and the
    shape follows as paths it can paste. ``folded_through`` is spelled out in words
    where it is absent, because "not read" and "nothing folded yet" are the two answers
    a reader must not merge, and a bare ``null`` beside a ``0`` invites exactly that.
    """
    rows = payload.get("types")
    if not isinstance(rows, list) or not rows:
        return (
            "No data type came back, so there is nothing to bind a dashboard block to "
            "yet. Tell the human: this gateway's fold registry answered empty."
        )
    lines = [
        f"{len(rows)} data types you can bind a dashboard block to. Each one is a fold "
        "of your crew log; bind a block by naming the type and a path from its list.",
    ]
    unread = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        source = row.get("source_fold")
        lines.append("")
        head = f"  `{name}` -- {row.get('keyed_by')}-keyed"
        if source and source != name:
            head += f", derived from the `{source}` fold"
        lines.append(head)
        seq = row.get("folded_through")
        if isinstance(seq, int):
            lines.append(f"    folded through seq {seq}")
        elif row.get("owner_served"):
            unread = True
            lines.append("    folded through: not read -- owner-served, see the note below")
        else:
            unread = True
            lines.append("    folded through: not read -- see the note below")
        paths = _flatten(row.get("shape"))
        shown = paths[:_MAX_PATHS_PER_TYPE]
        if shown:
            lines.append("    paths:")
            lines.extend(f"      {line}" for line in shown)
        if len(paths) > len(shown):
            lines.append(f"      ... and {len(paths) - len(shown)} more paths under this type")
    unknown = str(payload.get("unknown_type") or "unknown")
    lines.append("")
    if unread:
        # SAID ONCE, and said at all. "not read" and "folded through 0" are the two
        # answers a composing agent must not merge -- one is a fold it cannot see, the
        # other a fold that has consumed nothing -- and repeating the distinction on
        # every unread row would spend a screenful of the agent's context on one fact.
        owner = str(payload.get("owner_served") or "")
        lines.append(
            "A type marked 'not read' has a position this call did not fetch, which is "
            "NOT the same as 0: 0 means that fold has consumed nothing, 'not read' "
            "means nobody looked. You may still bind to it."
            + (
                f" The `{owner}` fold is always this way here: only its own owner "
                "serves it, so a generic read is refused by design."
                if owner
                else ""
            )
        )
    lines.append(
        f"A path typed `{unknown}` is one this catalog could not type: the fold renders "
        "null until something fills it. Binding one is allowed and it may draw nothing, "
        "so prefer a path with a real type."
    )
    return "\n".join(lines)


def _render_preview(preview: dict[str, Any]) -> str:
    """The staged page, and the sentence that makes the agent ASK before applying.

    The link leads, because it is the only part the person needs. The reminder to ask
    is last and unconditional: this tool's whole value is that it changes nothing, and
    an agent that previews and then applies in the same breath has spent the staging
    step without ever letting anybody look.
    """
    fields = preview.get("fields")
    lines = [
        f"Staged for a look -- NOTHING has changed yet. Give the person this link, "
        f"as written: [Open the dashboard preview]({preview.get('preview_url')})",
        "",
    ]
    lines.append(
        f"It is template `{preview.get('template_id')}` v"
        f"{preview.get('template_version')}, titled "
        f"{str(preview.get('title') or '')!r}."
    )
    if isinstance(fields, list) and fields:
        lines.append(f"It shows: {', '.join(str(f) for f in fields)}.")
    lines.append("")
    lines.append(
        f"dashboard_apply with template_id `{preview.get('template_id')}` puts it live. "
        "The person may also press Keep or Discard on the preview themselves, so "
        "re-read your [DASHBOARD] block before calling dashboard_apply."
    )
    return "\n".join(lines)


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
        note = payload.get("upkeep_note")
        if isinstance(note, str) and note:
            lines.append(note)
        rows = payload.get("fields")
        if isinstance(rows, list) and rows:
            lines.append("")
            lines.append("Fields:")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("source") == "agentic":
                    lines.append(f"  {row.get('field')} ({row.get('type')}) -- YOURS to write")
                    upkeep = row.get("upkeep")
                    if isinstance(upkeep, dict):
                        # What it means, when to write it, how -- and the helper that
                        # fetches the data when the crew log does not hold it.
                        lines.append(f"    means: {upkeep.get('means')}")
                        lines.append(f"    when: {upkeep.get('when')}")
                        lines.append(f"    how: {upkeep.get('how')}")
                        if upkeep.get("helper"):
                            lines.append(
                                f"    helper: python3 {upkeep['helper']} --help (prints the "
                                "dashboard_write payload; if you have no shell, write the "
                                "value you can read instead)"
                            )
                    schema = row.get("schema")
                    if isinstance(schema, dict) and set(schema) != {"type"}:
                        # The nested shape whole: the write is refused for any key
                        # it does not list, so the agent needs every key it does.
                        lines.append("    shape: " + json.dumps(schema, ensure_ascii=False))
                    if row.get("written") is True:
                        at = row.get("written_at")
                        lines.append(
                            "    current value"
                            + (f" (written {at})" if at else "")
                            + ": "
                            + json.dumps(row.get("value"), ensure_ascii=False)
                        )
                    elif row.get("written") is False:
                        lines.append("    current value: not written yet")
                else:
                    summary = row.get("summary")
                    lines.append(
                        f"  {row.get('field')} ({row.get('type')}) -- read from the "
                        f"{row.get('fold')} fold at {row.get('path')}"
                        + (f"; now: {summary}" if summary else "")
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
    history = payload.get("history")
    if isinstance(history, list) and history:
        lines.append("")
        lines.append("Your page's history, oldest first:")
        for row in history:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"  v{row.get('instance_version')}: {row.get('action')} "
                f"`{row.get('template_id')}`"
            )
    retained = payload.get("rollback_versions")
    if isinstance(retained, list) and retained:
        lines.append("")
        # The RETAINED versions, which is a shorter list than the history above: a
        # rollback can only reach a payload still on disk, and naming a version the
        # history mentions but the store has dropped is a refusal an agent can avoid
        # by reading this line.
        lines.append(
            "Versions dashboard_rollback can still reach: " + ", ".join(str(v) for v in retained)
        )
    budget = payload.get("retry_budget")
    if budget:
        lines.append("")
        lines.append(
            f"A refused write names the fields you could have used. Fix it from that "
            f"list and retry; after {budget} tries, ask the human."
        )
    return "\n".join(lines)


def _empty_value_for(field: str, sk: str) -> Any:
    """The value ``clear: true`` writes to *field*: ``{}`` for an object field, else ``[]``.

    The type comes from the field list ``dashboard_fields`` shows, which is the
    declared type the write route checks against. Nothing is refused here. The write
    route answers every clear: a string, number or boolean field with ``wrong_type``,
    a fold-sourced field with ``field_not_agentic`` and a field the list does not name
    with ``unknown_field``, each recorded in the mistake book. A refusal here would
    repeat that answer and keep it out of the book.

    A field list that cannot be read gets ``[]`` as well, so emptying an array field
    does not depend on the fields route answering. An object field cleared then is
    refused by the write route as ``wrong_type``, and the next clear that can read
    the list writes ``{}``.
    """
    d = _get("/api/agent-panel/dashboard/fields", session_key=sk)
    rows = None if d.get("error") else d.get("fields")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("field") == field:
                return {} if row.get("type") == "object" else []
    return []


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
