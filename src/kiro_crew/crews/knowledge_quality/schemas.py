"""Structured contracts for the read-only Knowledge Quality Crew."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any

_MAX_TEXT_CHARS = 2000
_MAX_ARRAY_ITEMS = 64
_MAX_HANDOFF_TEXT_CHARS = 256


def _text(max_length: int = _MAX_TEXT_CHARS) -> dict[str, Any]:
    return {"type": "string", "maxLength": max_length}


def _array(item_type: str = "string", *, max_items: int = _MAX_ARRAY_ITEMS) -> dict[str, Any]:
    item_schema: dict[str, Any] = {"type": item_type}
    if item_type == "string":
        item_schema["maxLength"] = _MAX_TEXT_CHARS
    return {"type": "array", "items": item_schema, "maxItems": max_items}


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _handoff_input() -> dict[str, Any]:
    return _object(
        {
            "handoff_id": _text(_MAX_HANDOFF_TEXT_CHARS),
            "source_role": _text(_MAX_HANDOFF_TEXT_CHARS),
            "artifact_type": _text(_MAX_HANDOFF_TEXT_CHARS),
            "schema_version": _text(_MAX_HANDOFF_TEXT_CHARS),
            "quality_status": _text(_MAX_HANDOFF_TEXT_CHARS),
            "payload": {"type": "object"},
        },
        [
            "handoff_id",
            "source_role",
            "artifact_type",
            "schema_version",
            "quality_status",
            "payload",
        ],
    )


KNOWLEDGE_QUALITY_SCHEMAS: Mapping[str, dict[str, Any]] = MappingProxyType(
    {
        "retrieval_audit_request": _object(
            {
                "scope": {"type": "object"},
                "observations": _array("object"),
                "runtime": {"type": "object"},
            },
            ["scope", "observations", "runtime"],
        ),
        "knowledge_audit_report": _object(
            {
                "scope": {"type": "object"},
                "pipeline_observations": _array(),
                "case_results": _array("object"),
                "retrieval_findings": _array(),
                "migration_rollback_risks": _array(),
                "unverified_claims": _array(),
                "next_actions": _array(),
            },
            [
                "scope",
                "pipeline_observations",
                "case_results",
                "retrieval_findings",
                "migration_rollback_risks",
                "unverified_claims",
                "next_actions",
            ],
        ),
        "validation_request": _object(
            {
                "scope": {"type": "object"},
                "observations": _array("object"),
                "knowledge_audit_report": _handoff_input(),
            },
            ["scope", "observations", "knowledge_audit_report"],
        ),
        "validation_report": _object(
            {
                "status": {
                    "type": "string",
                    "enum": ["passed", "failed", "blocked"],
                },
                "case_results": _array("object"),
                "evidence_gaps": _array(),
                "failures": _array(),
            },
            ["status", "case_results", "evidence_gaps", "failures"],
        ),
        "risk_review_request": _object(
            {
                "scope": {"type": "object"},
                "runtime": {"type": "object"},
                "validation_report": _handoff_input(),
            },
            ["scope", "runtime", "validation_report"],
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
    """Return an independent copy of one Knowledge Quality schema."""

    schema = KNOWLEDGE_QUALITY_SCHEMAS.get(name)
    if schema is None:
        raise KeyError(name)
    return deepcopy(schema)


__all__ = ["KNOWLEDGE_QUALITY_SCHEMAS", "schema_for"]
