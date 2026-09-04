"""The dynamic workflows: author, run, monitor, restart tools: what they advertise and what they do.

``schemas()`` returns the ADVERTISEMENT half of each tool -- its name, the
model-facing description, and the JSON Schema a call is validated against.
``HANDLERS`` maps each of those names to the function that runs it. Both halves
of a tool live here so its contract and its behavior are read together, and
``test_mcp_tool_registry`` fails if one arrives without the other.

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
from urllib.parse import quote

from kiro_crew import mcp_core
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import (
    WORKFLOW_AUTHOR_SCHEMA,
    WORKFLOW_LIBRARY_LIST_SCHEMA,
    WORKFLOW_RERUN_SCHEMA,
    WORKFLOW_RUN_ID_SCHEMA,
    WORKFLOW_RUN_SCHEMA,
    validate_tool_args,
)


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the workflows tools."""
    return [
        # --- Dynamic workflows (M6): author + run + monitor from chat ---
        {
            "name": "workflow_author",
            "description": (
                "Turn a natural-language goal into a runnable DYNAMIC WORKFLOW "
                "Python script (orchestrates agents via a sandboxed `ctx` DSL). "
                "Returns the validated script source — then call workflow_run to "
                "execute it. (Usually you can skip this and pass `intent` straight to "
                "workflow_run, which authors+runs in one step.)"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "The goal in plain language, e.g. 'deep research on the origin of pizza'",
                    },
                },
                "required": ["intent"],
            },
        },
        {
            "name": "workflow_run",
            "description": (
                "★ THE tool for 'use a dynamic workflow to …' / 'run a workflow' / any "
                "multi-phase, monitorable, restartable agent orchestration. PREFER THIS "
                "over spawn_sub_agents for such requests. Just pass `intent` (the user's "
                "goal in plain words) and it authors + launches the workflow in one step "
                "— do NOT hand-roll the orchestration with spawn tools. Returns a run_id "
                "immediately; the run streams to the Workflows dashboard tab and its "
                "result is injected back into this chat on completion. Monitor with "
                "workflow_status / workflow_result; restart parts with "
                "workflow_rerun_subtree."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Workflow script source (Python)"},
                    "workflow": {
                        "type": "string",
                        "description": "Saved workflow id or slug to run exactly",
                    },
                    "input": {
                        "type": "string",
                        "description": "Free-form input exposed as ctx.args['input']",
                    },
                    "intent": {
                        "type": "string",
                        "description": "If no source: a NL goal to author then run",
                    },
                    "name": {"type": "string", "description": "Optional run name"},
                    "args": {
                        "type": "object",
                        "description": "Optional args passed to the workflow",
                    },
                    "budget_total": {
                        "type": "integer",
                        "description": "Optional token budget ceiling for the run",
                    },
                },
            },
        },
        {
            "name": "workflow_library_list",
            "description": (
                "List reusable workflows explicitly saved in the global Kiro Crew library. "
                "Pass search to find local workflows relevant to a new request before authoring."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"search": {"type": "string"}},
            },
        },
        {
            "name": "workflow_status",
            "description": (
                "Get the live status of a background workflow run by run_id "
                "(running/finished/failed/cancelled + agent/event counts). Use to "
                "monitor a run you started; for the full result use workflow_result."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        },
        {
            "name": "workflow_result",
            "description": (
                "Get a workflow run's full result + event stream by run_id "
                "(phases, per-agent outcomes, logs, final result). For a run that "
                "ended without a usable return value, also returns the agent "
                "payloads that completed first as `partial_results` and any "
                "per-agent failure reasons as `agent_errors`."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        },
        {
            "name": "workflow_list",
            "description": "List recent background workflow runs (newest first) with their status.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "workflow_cancel",
            "description": "Cancel a running background workflow by run_id.",
            "inputSchema": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        },
        {
            "name": "workflow_rerun_subtree",
            "description": (
                "Re-run a prior workflow, REPLAYING the unchanged prefix and "
                "re-executing from a chosen step ('restart parts' at runtime). "
                "Agent calls before `from_index` reuse the prior run's cached "
                "results; calls at/after re-call the model. from_index=0 re-runs "
                "everything fresh. Returns a new run_id."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "The prior run to restart from"},
                    "from_index": {
                        "type": "integer",
                        "description": "Agent call_index to restart at (0 = full re-run)",
                        "default": 0,
                    },
                },
                "required": ["run_id"],
            },
        },
    ]


def _redact_obj(obj: Any) -> Any:
    """Recursively redact credentials + exfiltration URLs from a response.

    Keys are redacted too: agent output is parsed into these structures, so a
    credential can arrive as a mapping key and a values-only walk would let it
    through (see dashboard/handlers/workflows.py::_redact_obj).
    """
    if isinstance(obj, str):
        s, _ = redact_exfiltration_urls(obj)
        s, _ = redact_credentials(s)
        return s
    if isinstance(obj, list):
        return [_redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {_redact_obj(k): _redact_obj(v) for k, v in obj.items()}
    return obj


def _wf_return(tool: str, text: str, *, outcome: str = "success") -> str:
    safe, _ = redact_exfiltration_urls(text)
    safe, _ = redact_credentials(safe)
    mcp_core.sel().log_tool_invocation(
        session_key=mcp_core._resolve_session_key(),
        source="mcp",
        tool_name=tool,
        outcome=outcome,
    )
    return safe


def workflow_author(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, WORKFLOW_AUTHOR_SCHEMA)
    intent = (args.get("intent") or "").strip()
    if not intent:
        return _wf_return("workflow_author", "Error: intent is required", outcome="error")
    d = mcp_core._post("/api/workflows/author", {"intent": intent})
    if d.get("error"):
        return _wf_return(
            "workflow_author", f"workflow_author failed: {d['error']}", outcome="error"
        )
    if not d.get("ok"):
        return _wf_return(
            "workflow_author",
            "Could not author a valid workflow: " + "; ".join(d.get("errors", [])),
            outcome="error",
        )
    return _wf_return(
        "workflow_author",
        "Authored workflow. Review then run it with workflow_run(source=…):\n\n"
        f"{d.get('source', '')}",
    )


def workflow_run(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, WORKFLOW_RUN_SCHEMA)
    source = args.get("source") or ""
    workflow_ref = (args.get("workflow") or "").strip()
    intent = (args.get("intent") or "").strip()
    if not (source or workflow_ref or intent):
        return _wf_return(
            "workflow_run", "Error: provide either 'source' or 'intent'", outcome="error"
        )
    wf_body: dict[str, Any] = {}
    if args.get("name"):
        wf_body["name"] = args["name"]
    if isinstance(args.get("args"), dict):
        wf_body["args"] = args["args"]
    if isinstance(args.get("budget_total"), int):
        wf_body["budget_total"] = args["budget_total"]
    if workflow_ref:
        session_key, _strict_err = mcp_core.require_strict_session_key(
            "Error: cannot verify caller identity for workflow_run. Refusing to start a "
            "session-bound workflow."
        )
        if not session_key:
            return _wf_return(
                "workflow_run",
                _strict_err,
                outcome="error",
            )
        if args.get("input"):
            wf_body["input"] = args["input"]
        d = mcp_core._post(
            f"/api/workflows/definitions/{quote(workflow_ref, safe='')}/run",
            wf_body,
            session_key=session_key,
        )
        if d.get("error"):
            return _wf_return("workflow_run", f"workflow_run failed: {d['error']}", outcome="error")
        return _wf_return(
            "workflow_run",
            f"Started saved workflow `/workflow {workflow_ref}` as `{d.get('run_id')}` "
            f"from revision {d.get('revision')}. Its result will be injected here on completion.",
        )
    if not source and intent:
        # Author-in-run (M6.7): returns a run_id INSTANTLY — the script is
        # authored inside the background run as a visible "Authoring" phase, so
        # the slow model call never blocks this tool (no 30s author timeout).
        wf_body["intent"] = intent
        d = mcp_core._post("/api/workflows/run_intent", wf_body)
        if d.get("error"):
            return _wf_return("workflow_run", f"workflow_run failed: {d['error']}", outcome="error")
        return _wf_return(
            "workflow_run",
            f"Started workflow run `{d.get('run_id')}`. It is authoring the workflow "
            "from your request now (watch the Authoring phase in the Workflows tab / "
            "chat activity), then runs in the background. Its result will be injected "
            f"here on completion — or check progress with workflow_status('{d.get('run_id')}').",
        )
    wf_body["source"] = source
    d = mcp_core._post("/api/workflows/run", wf_body)
    if d.get("error"):
        return _wf_return("workflow_run", f"workflow_run failed: {d['error']}", outcome="error")
    return _wf_return(
        "workflow_run",
        f"Started workflow run `{d.get('run_id')}` (name: {d.get('name') or '—'}). "
        "It runs in the background — monitor with workflow_status, and its result "
        "will be injected here on completion. You can keep working; check back with "
        f"workflow_status('{d.get('run_id')}').",
    )


def workflow_library_list(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, WORKFLOW_LIBRARY_LIST_SCHEMA)
    search = (args.get("search") or "").strip()
    path = "/api/workflows/definitions"
    if search:
        path += f"?q={quote(search, safe='')}"
    d = mcp_core._get(path)
    if d.get("error"):
        return _wf_return(
            "workflow_library_list", f"workflow_library_list: {d['error']}", outcome="error"
        )
    definitions = d.get("definitions", [])
    if not definitions:
        return _wf_return("workflow_library_list", "No saved workflows yet.")
    lines = [
        f"- `/workflow {item.get('slug')}` — {item.get('name') or item.get('slug')} "
        f"(revision {item.get('revision')}, id `{item.get('id')}`)"
        for item in definitions
    ]
    return _wf_return("workflow_library_list", "Saved workflows:\n" + "\n".join(lines))


def workflow_status(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, WORKFLOW_RUN_ID_SCHEMA)
    run_id = args.get("run_id", "")
    d = mcp_core._get(f"/api/workflows/runs/{run_id}")
    # A *failed* run's snapshot legitimately carries its own ``error`` field
    # (its failure message) alongside ``run_id`` — that is NOT a transport
    # error. Only bail early when the response is a bare transport/404 error
    # (``{"error": ...}`` with no ``run_id``); otherwise report the run,
    # including its failure message.
    if d.get("error") and "run_id" not in d:
        return _wf_return("workflow_status", f"workflow_status: {d['error']}", outcome="error")
    # ``error`` (and ``name``) are LLM-derived — redact before surfacing them
    # to the dashboard/chat (credentials + exfiltration URLs).
    safe_err = _redact_obj(d["error"]) if d.get("error") else ""
    safe_name = _redact_obj(d.get("name") or "—")
    return _wf_return(
        "workflow_status",
        f"Run `{d.get('run_id')}` ({safe_name}): **{d.get('status')}** "
        f"— {d.get('event_count', 0)} events" + (f"; error: {safe_err}" if safe_err else ""),
    )


def workflow_result(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, WORKFLOW_RUN_ID_SCHEMA)
    run_id = args.get("run_id", "")
    d = mcp_core._get(f"/api/workflows/runs/{run_id}")
    # As in workflow_status: a failed run carries its own ``error`` in the
    # snapshot. Distinguish a real transport error (no ``run_id``) from a
    # failed-but-readable run so a failed run still returns its full event
    # stream instead of masquerading as a transport failure.
    if d.get("error") and "run_id" not in d:
        return _wf_return("workflow_result", f"workflow_result: {d['error']}", outcome="error")
    # ``result`` / ``error`` / ``events`` are LLM-derived (agent outputs, log
    # lines) — recursively redact credentials + exfiltration URLs before
    # returning them through this MCP tool to the dashboard/chat surface.
    # ``partial_results`` / ``agent_errors`` carry the same class of content and
    # MUST be projected here: when a run ends without a usable return value they
    # are the only surviving output, and the completion message points the reader
    # at this tool to read them. Omitting them made that instruction a dead end.
    # Oversize payloads are handled by the MCP gateway's existing result spill.
    wf_payload: dict[str, Any] = {
        "run_id": d.get("run_id"),
        "status": d.get("status"),
        "result": _redact_obj(d.get("result")),
        "error": _redact_obj(d.get("error")),
        "events": _redact_obj(d.get("events", [])),
    }
    if d.get("partial_results"):
        wf_payload["partial_results"] = _redact_obj(d.get("partial_results"))
    if d.get("agent_errors"):
        wf_payload["agent_errors"] = _redact_obj(d.get("agent_errors"))
    return _wf_return(
        "workflow_result",
        json.dumps(wf_payload, indent=2, default=str),
    )


def workflow_list(name: str, args: dict[str, Any]) -> str:
    d = mcp_core._get("/api/workflows/runs")
    if d.get("error"):
        return _wf_return("workflow_list", f"workflow_list: {d['error']}", outcome="error")
    runs = d.get("runs", [])
    if not runs:
        return _wf_return("workflow_list", "No workflow runs yet.")
    lines = [
        f"- `{r.get('run_id')}` {r.get('name') or '—'} → {r.get('status')} "
        f"({r.get('event_count', 0)} events)"
        for r in runs
    ]
    return _wf_return("workflow_list", "Workflow runs (newest first):\n" + "\n".join(lines))


def workflow_cancel(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, WORKFLOW_RUN_ID_SCHEMA)
    run_id = args.get("run_id", "")
    d = mcp_core._post(f"/api/workflows/runs/{run_id}/cancel", {})
    if d.get("error"):
        return _wf_return("workflow_cancel", f"workflow_cancel: {d['error']}", outcome="error")
    return _wf_return(
        "workflow_cancel",
        f"Run `{run_id}`: {'cancelled' if d.get('cancelled') else 'not cancellable (already done?)'}",
    )


def workflow_rerun_subtree(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, WORKFLOW_RERUN_SCHEMA)
    run_id = args.get("run_id", "")
    from_index = args.get("from_index", 0)
    d = mcp_core._post(
        f"/api/workflows/runs/{run_id}/rerun",
        {"from_index": from_index if isinstance(from_index, int) else 0},
    )
    if d.get("error"):
        return _wf_return(
            "workflow_rerun_subtree", f"workflow_rerun_subtree: {d['error']}", outcome="error"
        )
    return _wf_return(
        "workflow_rerun_subtree",
        f"Re-running `{run_id}` as `{d.get('run_id')}` "
        f"(replaying calls before index {d.get('replayed_before')}). "
        f"Monitor with workflow_status('{d.get('run_id')}').",
    )


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "workflow_author": workflow_author,
    "workflow_run": workflow_run,
    "workflow_library_list": workflow_library_list,
    "workflow_status": workflow_status,
    "workflow_result": workflow_result,
    "workflow_list": workflow_list,
    "workflow_cancel": workflow_cancel,
    "workflow_rerun_subtree": workflow_rerun_subtree,
}
