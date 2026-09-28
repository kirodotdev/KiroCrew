"""Smallest-valid-payload vehicles for the session-log entry types.

A test that asserts on segments, ownership or the append path needs a well-formed
entry of some type without caring what it says. Building that payload from the
declarations in :mod:`kiro_crew.crew_log.entry_types` rather than writing it out
keeps a caller from holding a payload the format refuses.

This lives in the test suite, not beside the declarations, because every caller is
a test: the production write path is handed real data by its emitter.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.crew_log.entry_types import (
    JSON_ARRAY,
    JSON_BOOL,
    JSON_FLOAT,
    JSON_INT,
    JSON_STRING,
    Field,
    declaration_for,
)


def minimal_data(kind: str, entry_type: str) -> dict[str, Any]:
    """The smallest ``data`` this type accepts: its required fields, zero-valued.

    An enum field takes its first declared value, since a zero-length string is
    not a member of any vocabulary. An undeclared type has no requirements, so it
    gets an empty payload.
    """
    spec = declaration_for(kind, entry_type)
    if spec is None:
        return {}
    return {field.name: _zero(field) for field in spec.fields if field.required}


def _zero(spec: Field) -> Any:
    if spec.enum:
        return spec.enum[0]
    if spec.json_type == JSON_STRING:
        return ""
    if spec.json_type == JSON_INT:
        return 0
    if spec.json_type == JSON_FLOAT:
        return 0.0
    if spec.json_type == JSON_BOOL:
        return False
    if spec.json_type == JSON_ARRAY:
        return []
    return {member.name: _zero(member) for member in spec.fields if member.required}
