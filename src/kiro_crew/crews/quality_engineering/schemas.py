"""Bounded structured contracts for the Quality Engineering Crew."""

from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType
from typing import Any


def _array(item: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "array", "items": item or {"type": "string"}}


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


_EVIDENCE = _object(
    {
        "check_id": {"type": "string"},
        "adapter_id": {"type": "string"},
        "status": {"type": "string", "enum": ["passed", "failed", "blocked"]},
        "evidence_path": {"type": "string"},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
        "blocked_reason": {"type": "string"},
    },
    ["check_id", "status"],
)


QUALITY_ENGINEERING_SCHEMAS = MappingProxyType(
    {
        "quality_request": _object(
            {
                "request": {"type": "string"},
                "project_path": {"type": "string"},
                "changed_paths": _array(),
                "acceptance_criteria": _array(),
                "check_ids": _array(),
                "route": {"type": "string"},
                "evidence_root": {"type": "string"},
            },
            ["request", "project_path"],
        ),
        "qa_request": _object(
            {
                "request": {"type": "string"},
                "project_path": {"type": "string"},
                "changed_paths": _array(),
                "acceptance_criteria": _array(),
            },
            ["request", "project_path"],
        ),
        "qa_plan": _object(
            {
                "status": {"type": "string", "enum": ["passed", "blocked"]},
                "scope": {"type": "string"},
                "test_cases": _array(),
                "risks": _array(),
                "required_evidence": _array(),
                "findings": _array(),
            },
            ["status", "scope", "test_cases", "risks", "required_evidence", "findings"],
        ),
        "e2e_request": _object(
            {
                "request": {"type": "string"},
                "project_path": {"type": "string"},
                "check_ids": _array(),
                "evidence": _array(_EVIDENCE),
            },
            ["request", "project_path", "check_ids", "evidence"],
        ),
        "e2e_report": _object(
            {
                "status": {"type": "string", "enum": ["passed", "failed", "blocked"]},
                "checks": _array(),
                "evidence_refs": _array(),
                "findings": _array(),
                "blocked_reason": {"type": "string"},
            },
            ["status", "checks", "evidence_refs", "findings", "blocked_reason"],
        ),
        "ux_request": _object(
            {
                "request": {"type": "string"},
                "project_path": {"type": "string"},
                "acceptance_criteria": _array(),
                "evidence_refs": _array(),
            },
            ["request", "project_path", "acceptance_criteria", "evidence_refs"],
        ),
        "ux_review": _object(
            {
                "status": {"type": "string", "enum": ["passed", "failed", "blocked"]},
                "findings": _array(),
                "accessibility_checks": _array(),
                "usability_checks": _array(),
                "evidence_refs": _array(),
            },
            ["status", "findings", "accessibility_checks", "usability_checks", "evidence_refs"],
        ),
        "quality_report": _object(
            {
                "status": {"type": "string", "enum": ["passed", "failed", "blocked"]},
                "route": {"type": "string"},
                "role_statuses": _array(),
                "findings": _array(),
                "evidence_refs": _array(),
                "blocked_reason": {"type": "string"},
            },
            ["status", "route", "role_statuses", "findings", "evidence_refs", "blocked_reason"],
        ),
    }
)


def schema_for(name: str) -> dict[str, Any]:
    """Return an independent copy of one Quality Engineering schema."""

    schema = QUALITY_ENGINEERING_SCHEMAS.get(name)
    if schema is None:
        raise KeyError(name)
    return deepcopy(schema)


__all__ = ["QUALITY_ENGINEERING_SCHEMAS", "schema_for"]
