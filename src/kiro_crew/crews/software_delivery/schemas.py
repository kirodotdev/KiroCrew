"""Structured input and output schemas for the Software Delivery Crew."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any


def _array() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


SOFTWARE_DELIVERY_SCHEMAS: Mapping[str, dict[str, Any]] = MappingProxyType(
    {
        "architecture_request": _object(
            {
                "request": {"type": "string"},
                "constraints": _array(),
                "acceptance_criteria": _array(),
                "candidate_workspace": {"type": "string"},
            },
            ["request"],
        ),
        "architecture_brief": _object(
            {
                "request": {"type": "string"},
                "problem": {"type": "string"},
                "goals": _array(),
                "non_goals": _array(),
                "constraints": _array(),
                "options": _array(),
                "decision": {"type": "string"},
                "affected_components": _array(),
                "acceptance_criteria": _array(),
                "risks": _array(),
                "rollback_plan": {"type": "string"},
            },
            [
                "request",
                "problem",
                "goals",
                "non_goals",
                "constraints",
                "options",
                "decision",
                "affected_components",
                "acceptance_criteria",
                "risks",
                "rollback_plan",
            ],
        ),
        "implementation_request": _object(
            {
                "request": {"type": "string"},
                "candidate_workspace": {"type": "string"},
                "architecture_brief": {"type": "object"},
            },
            ["request", "candidate_workspace"],
        ),
        "implementation_result": _object(
            {
                "changed_paths": _array(),
                "tests": _array(),
                "limitations": _array(),
            },
            ["changed_paths", "tests", "limitations"],
        ),
        "validation_request": _object(
            {
                "request": {"type": "string"},
                "candidate_workspace": {"type": "string"},
                "architecture_brief": {"type": "object"},
                "implementation_result": {"type": "object"},
            },
            ["request", "implementation_result"],
        ),
        "validation_report": _object(
            {
                "status": {"type": "string", "enum": ["passed", "failed", "blocked"]},
                "checks": _array(),
                "failures": _array(),
            },
            ["status", "checks", "failures"],
        ),
        "risk_review_request": _object(
            {
                "request": {"type": "string"},
                "changed_paths": _array(),
                "validation_report": {"type": "object"},
            },
            ["request", "changed_paths", "validation_report"],
        ),
        "risk_review": _object(
            {
                "status": {
                    "type": "string",
                    "enum": ["passed", "changes_requested", "blocked"],
                },
                "findings": _array(),
                "residual_risk": {"type": "string"},
            },
            ["status", "findings", "residual_risk"],
        ),
    }
)


def schema_for(name: str) -> dict[str, Any]:
    """Return an independent copy of one Crew schema."""

    schema = SOFTWARE_DELIVERY_SCHEMAS.get(name)
    if schema is None:
        raise KeyError(name)
    return deepcopy(schema)


__all__ = ["SOFTWARE_DELIVERY_SCHEMAS", "schema_for"]
