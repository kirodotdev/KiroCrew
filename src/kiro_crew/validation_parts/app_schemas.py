"""Tool schemas for the builtin apps' agent tools.

The Ops Mission Control API allowlist. A tool an installed app declares in its manifest
carries its own schema (``apps.app_agent_tools.build_tool_schema``).

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
