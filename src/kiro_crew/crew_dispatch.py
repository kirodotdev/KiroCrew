"""Internal native dispatch for automatic Crew routing.

The dashboard starts one ordinary Dynamic Workflow. That workflow calls
``ctx.workflow`` once with one of the private names below; this adapter invokes
the package directly on the same WorkflowContext. No public MCP workflow tool
is called and no second orchestration engine is introduced.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kiro_crew.config.paths import config_dir
from kiro_crew.security import redact_and_truncate

INTERNAL_SOFTWARE_WORKFLOW = "__kirocrew.crew.software-delivery"
INTERNAL_KNOWLEDGE_WORKFLOW = "__kirocrew.crew.knowledge-quality"
INTERNAL_QUALITY_WORKFLOW = "__kirocrew.crew.quality-engineering"
INTERNAL_WORKFLOW_NAMES = frozenset(
    {INTERNAL_SOFTWARE_WORKFLOW, INTERNAL_KNOWLEDGE_WORKFLOW, INTERNAL_QUALITY_WORKFLOW}
)

_MAX_DEPTH = 8
_MAX_ITEMS = 64
_MAX_KEYS = 64
_MAX_TEXT = 2000


def automatic_workflow_source() -> str:
    """Return the validated, fixed script used for one automatic route."""

    return """META = {"name": "automatic-crew-routing", "description": "Private automatic Crew route", "phases": ["route"]}

async def workflow(ctx):
    ctx.phase("Automatic Crew route")
    return await ctx.workflow(ctx.args.get("__crew_workflow"), ctx.args)
"""


def _redact_bounded(value: object, *, depth: int = 0) -> object:
    """Make a JSON-compatible, redacted, bounded result envelope."""

    if depth > _MAX_DEPTH:
        return "[withheld: payload depth exceeded]"
    if isinstance(value, str):
        return redact_and_truncate(value, max_chars=_MAX_TEXT)
    if isinstance(value, Mapping):
        return {
            redact_and_truncate(str(key), max_chars=256): _redact_bounded(item, depth=depth + 1)
            for key, item in list(value.items())[:_MAX_KEYS]
        }
    if isinstance(value, (list, tuple)):
        return [_redact_bounded(item, depth=depth + 1) for item in list(value)[:_MAX_ITEMS]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_and_truncate(str(value), max_chars=_MAX_TEXT)


def _result_dict(result: Any) -> dict[str, Any]:
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
    elif isinstance(result, Mapping):
        value = dict(result)
    else:
        value = {"status": "blocked", "blocked_reason": "crew.result.invalid"}
    bounded = _redact_bounded(value)
    return (
        bounded
        if isinstance(bounded, dict)
        else {"status": "blocked", "blocked_reason": "crew.result.invalid"}
    )


def _default_knowledge_request(args: Mapping[str, Any]) -> dict[str, Any]:
    """Build the read-only audit request from the authoritative local store."""

    from kiro_crew.crews.knowledge_quality import load_audit_cases

    raw_limit = args.get("limit", 5)
    limit = raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else 5
    return {
        "database_path": str(config_dir() / "workspace" / "knowledge" / "knowledge.db"),
        "cases": list(load_audit_cases()),
        "limit": max(1, min(limit, 20)),
        "embedding_mode": "configured",
    }


def _software_request(args: Mapping[str, Any]) -> dict[str, Any]:
    raw_request = args.get("request")
    request_text = raw_request if isinstance(raw_request, str) else ""
    candidate = args.get("candidate_workspace")
    candidate_text = candidate if isinstance(candidate, str) else ""
    request: dict[str, Any] = {
        "request": redact_and_truncate(request_text, max_chars=_MAX_TEXT),
        "candidate_workspace": candidate_text,
    }
    constraints = args.get("constraints")
    if isinstance(constraints, list):
        request["constraints"] = [
            redact_and_truncate(item, max_chars=512)
            for item in constraints[:16]
            if isinstance(item, str)
        ]
    return request


def _bounded_text_list(args: Mapping[str, Any], key: str, *, limit: int = 16) -> list[str]:
    values = args.get(key)
    if not isinstance(values, list):
        return []
    return [
        redact_and_truncate(item.strip(), max_chars=512)
        for item in values[:limit]
        if isinstance(item, str) and item.strip()
    ]


def _quality_request(args: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize only the Quality Engineering contract; drop command-shaped fields."""

    from kiro_crew.crews.quality_engineering import DEFAULT_E2E_CHECK_IDS

    raw_request = args.get("request")
    raw_project = args.get("project_path", args.get("candidate_workspace"))
    raw_checks = _bounded_text_list(args, "check_ids", limit=8)
    request: dict[str, Any] = {
        "request": (
            redact_and_truncate(raw_request.strip(), max_chars=_MAX_TEXT)
            if isinstance(raw_request, str)
            else ""
        ),
        "project_path": raw_project.strip() if isinstance(raw_project, str) else "",
        "changed_paths": _bounded_text_list(args, "changed_paths"),
        "acceptance_criteria": _bounded_text_list(args, "acceptance_criteria"),
        "check_ids": raw_checks or list(DEFAULT_E2E_CHECK_IDS),
        "route": args.get("route") if isinstance(args.get("route"), str) else "full_quality_review",
    }
    evidence_root = args.get("evidence_root")
    if isinstance(evidence_root, str) and evidence_root.strip():
        request["evidence_root"] = evidence_root.strip()
    return request


async def execute_native_crew(
    ctx: Any, workflow_name: str, args: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Execute one private Crew package on the parent workflow context."""

    if workflow_name not in INTERNAL_WORKFLOW_NAMES:
        return {"status": "blocked", "blocked_reason": "crew.workflow.unknown"}
    payload = args if isinstance(args, Mapping) else {}
    workflow_id = str(getattr(ctx, "_run_id", "automatic-crew-routing"))
    source_session = str(getattr(ctx, "_session_key", ""))
    route = payload.get("route")
    route_name = route if isinstance(route, str) and route.strip() else ""
    model = payload.get("model")

    if workflow_name == INTERNAL_SOFTWARE_WORKFLOW:
        from kiro_crew.crews.software_delivery import SoftwareDeliveryCrew

        software_result = await SoftwareDeliveryCrew().run(
            ctx,
            request=_software_request(payload),
            route=route_name,
            workflow_id=workflow_id,
            source_session=source_session,
            approval_prompt="Approve the production-change Software Delivery route before role execution.",
        )
        return _result_dict(software_result)

    if workflow_name == INTERNAL_QUALITY_WORKFLOW:
        from kiro_crew.crews.quality_engineering import QualityEngineeringCrew

        quality_result = await QualityEngineeringCrew().run(
            ctx,
            request=_quality_request(payload),
            route=route_name or "full_quality_review",
            workflow_id=workflow_id,
            source_session=source_session,
            model=model if isinstance(model, str) and model.strip() else None,
        )
        return _result_dict(quality_result)

    from kiro_crew.crews.knowledge_quality import KnowledgeQualityCrew

    knowledge_result = await KnowledgeQualityCrew().run(
        ctx,
        request=_default_knowledge_request(payload),
        route=route_name,
        workflow_id=workflow_id,
        source_session=source_session,
        model=model if isinstance(model, str) and model.strip() else None,
    )
    return _result_dict(knowledge_result)


__all__ = [
    "INTERNAL_KNOWLEDGE_WORKFLOW",
    "INTERNAL_QUALITY_WORKFLOW",
    "INTERNAL_SOFTWARE_WORKFLOW",
    "INTERNAL_WORKFLOW_NAMES",
    "automatic_workflow_source",
    "execute_native_crew",
]
