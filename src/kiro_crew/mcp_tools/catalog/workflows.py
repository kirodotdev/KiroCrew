"""Tool declarations of the ``workflows`` domain of ``kirocrew-core``.

The handlers are in :mod:`kiro_crew.mcp_tools.workflows`, which reads every name here as
its own. This module imports none of them (see the package docstring).
"""

from __future__ import annotations

from typing import Any


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
                "Run a DYNAMIC WORKFLOW: a sandboxed script that orchestrates agents "
                "in phases, streams to the Workflows dashboard tab, and can be "
                "restarted from any step. Choose it yourself, without waiting for the "
                "user to name it, when a task has several dependent phases or many "
                "independent pieces AND a failed piece should re-run without redoing "
                "the rest (e.g. research many sources, then synthesize; one audit "
                "applied across many files). A single task needs neither; a one-shot "
                "fan-out whose results you need only once fits spawn_run. Pass a saved "
                "slug as `workflow`, or `intent` (the goal in plain words) to author "
                "+ launch in one step — do not hand-roll the "
                "same orchestration with spawn tools. Returns a run_id immediately; "
                "the result is injected back into this chat on completion. Monitor "
                "with workflow_status / workflow_result; restart parts with "
                "workflow_rerun_subtree. Check workflow_library_list for a saved "
                "workflow before authoring a new one."
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
                        "description": (
                            "Free-form input exposed as ctx.args['input']. Used only "
                            "with workflow; with source or intent it is dropped, so "
                            "pass values in args instead"
                        ),
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
                    "title": {
                        "type": "string",
                        "description": (
                            "Optional title for the fallback `workflow-<run_id>` session this "
                            "run gets when its originating chat is gone (e.g. a webhook-driven "
                            "run). Lets many runs of one workflow be told apart in the sidebar. "
                            "Ignored when the originating chat is still live. Omit to keep the "
                            "default `Workflow: <name>`."
                        ),
                    },
                    "folder": {
                        "type": "string",
                        "description": (
                            "Optional EXISTING sidebar folder (id or '/'-separated path, "
                            "like session_create's `folder`) for the fallback "
                            "`workflow-<run_id>` session. Must already exist — nothing is "
                            "created; a missing or ambiguous folder rejects the run. "
                            "Ignored when the originating chat is still live. Omit to leave "
                            "the fallback unfiled."
                        ),
                    },
                },
            },
        },
        {
            "name": "workflow_library_list",
            "description": (
                "List reusable workflows explicitly saved in the global Kiro Crew library. "
                "Pass search to find local workflows relevant to a new request before authoring. "
                "To save one or revise a saved one, ask the user with "
                "propose_change(kind='workflow.save' or 'workflow.update')."
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
