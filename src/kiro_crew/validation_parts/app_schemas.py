"""Tool schemas for the builtin apps' agent tools.

The Ops Mission Control API allowlist and the pod tools. A tool an installed app declares in its manifest carries its own
schema (``apps.app_agent_tools.build_tool_schema``).

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import re
from typing import Any

from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema, ValidationError

# ── Tool Schemas (Ops Mission Control app) ──
#
# ``ops_mission_control_api`` is the agent's ONLY credentialed path to the
# app's HTTP surface (the MCP server process holds the internal secret; the
# agent never sees a credential). The (method, path) allowlist below is the entire authorization
# story for the tool, so it is defined here — next to the schema that
# enforces it — and imported by both the tool handler and the tests. It
# deliberately covers only what the app's SOPs need and EXCLUDES the
# human-decision and configuration routes (``/incident/proposal/decide``,
# ``/incident/propose``, ``/proposals``, ``/providers*``, ``/settings``,
# ``/webhook``, bare ``/incident``): an agent that needs one of those is by
# definition off-SOP.

OPS_MISSION_CONTROL_ALLOWED_CALLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/state"),
        ("GET", "/signals"),
        ("GET", "/incidents"),
        ("GET", "/handover"),
        ("GET", "/rotation"),
        ("GET", "/ledger"),
        ("GET", "/ledger/contradictions"),
        ("POST", "/dispatch"),
        ("POST", "/incident/transition"),
        ("POST", "/incident/claim"),
        ("POST", "/incident/action"),
        ("POST", "/rotation/arm"),
        ("POST", "/ledger"),
        ("POST", "/ledger/hygiene"),
    }
)
_OMC_API_METHODS = frozenset({"GET", "POST"})
_OMC_API_PATHS = frozenset(p for _, p in OPS_MISSION_CONTROL_ALLOWED_CALLS)
# Query strings are value-position only: no '/', '?' or '#', so a query can
# never rewrite the path it is appended to. '%' admits URL-encoded values.
_OMC_QUERY_RE = re.compile(r"^[A-Za-z0-9_.=&%+,:-]*$")
# Bounds the JSON body an agent can push through the tool. Ledger entries are
# the largest legitimate payload; 32 KiB is ~4x the biggest one observed.
_OMC_MAX_BODY = 32_768


def _validate_omc_api(cleaned: dict[str, Any]) -> None:
    method = cleaned.get("method")
    path = cleaned.get("path")
    if (method, path) not in OPS_MISSION_CONTROL_ALLOWED_CALLS:
        raise ValidationError("path", f"{method} {path} is not part of the agent surface")
    if cleaned.get("query") and method != "GET":
        raise ValidationError("query", "query is only accepted on GET calls")
    if cleaned.get("body_json") and method == "GET":
        raise ValidationError("body_json", "GET calls take no body")


OPS_MISSION_CONTROL_API_SCHEMA = ToolSchema(
    tool_name="ops_mission_control_api",
    fields=[
        FieldSpec("method", str, required=True, allowed=_OMC_API_METHODS),
        FieldSpec("path", str, required=True, allowed=_OMC_API_PATHS),
        FieldSpec("query", str, max_len=512, pattern=_OMC_QUERY_RE, default=""),
        FieldSpec("body_json", str, max_len=_OMC_MAX_BODY, default=""),
    ],
    custom_validator=_validate_omc_api,
)

# Dev Fleet pod lifecycle (agent surface). A pod name is a git worktree basename,
# so it reaches `git worktree list` matching and a filesystem path before
# `rt.validate_name` -- the real authority -- ever sees it. Bounded here so an
# unbounded string is refused at the tool boundary rather than carried further.
_POD_WORKTREE_MAX_LEN = 200

# `memory` / `cpu` are validated by `kirocrew pod up` (syntax, floor, host cap);
# the bound here only stops an unbounded string reaching the CLI's argv.
_POD_LIMIT_MAX_LEN = 16

POD_UP_SCHEMA = ToolSchema(
    tool_name="pod_up",
    fields=[
        FieldSpec("worktree", str, required=True, max_len=_POD_WORKTREE_MAX_LEN),
        FieldSpec("memory", str, max_len=_POD_LIMIT_MAX_LEN, default=""),
        FieldSpec("cpu", str, max_len=_POD_LIMIT_MAX_LEN, default=""),
    ],
)

POD_RESTART_SCHEMA = ToolSchema(
    tool_name="pod_restart",
    fields=[
        FieldSpec("worktree", str, required=True, max_len=_POD_WORKTREE_MAX_LEN),
        FieldSpec("memory", str, max_len=_POD_LIMIT_MAX_LEN, default=""),
        FieldSpec("cpu", str, max_len=_POD_LIMIT_MAX_LEN, default=""),
    ],
)

POD_DOWN_SCHEMA = ToolSchema(
    tool_name="pod_down",
    fields=[FieldSpec("worktree", str, required=True, max_len=_POD_WORKTREE_MAX_LEN)],
)

POD_STATUS_SCHEMA = ToolSchema(
    tool_name="pod_status",
    fields=[FieldSpec("worktree", str, required=True, max_len=_POD_WORKTREE_MAX_LEN)],
)

# `pod_ls` takes nothing. It is registered anyway: a tool ABSENT from
# MCP_CORE_SCHEMAS has its arguments passed through raw, so an empty schema is
# what makes an unexpected argument an "Error:" string instead of unvalidated input.
POD_LS_SCHEMA = ToolSchema(tool_name="pod_ls", fields=[])
