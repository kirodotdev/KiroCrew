"""RED test: validate_field accepts bool where a strict int is required.

bool is a subclass of int, so ``isinstance(True, int)`` is True. A bool
``task_id`` therefore slips through a strict-int field spec (True == 1, which
is neither < min_val=1 nor > max_val=999999).
"""

from __future__ import annotations

import pytest

from kiro_crew.validation import (
    FieldSpec,
    ToolSchema,
    ValidationError,
    validate_field,
    validate_tool_args,
)

# A strict-int schema fixture (an int task_id required in [1, 999999]).
_STRICT_INT_SCHEMA = ToolSchema(
    tool_name="strict_int",
    fields=[FieldSpec("task_id", int, required=True, min_val=1, max_val=999999)],
)


def test_agent_defect() -> None:
    # A bool must be rejected for a strict-int field, not silently accepted.
    with pytest.raises(ValidationError):
        validate_tool_args({"task_id": True}, _STRICT_INT_SCHEMA)

    # Direct field-level check too (bool not in the spec's allowed types).
    spec = FieldSpec("task_id", int, required=True, min_val=1, max_val=999999)
    with pytest.raises(ValidationError):
        validate_field(True, spec)
    with pytest.raises(ValidationError):
        validate_field(False, spec)

    # Genuine bool fields must still accept bool values.
    bool_spec = FieldSpec("silent", bool)
    assert validate_field(True, bool_spec) is True
    assert validate_field(False, bool_spec) is False

    # Real integers still pass.
    assert validate_field(42, spec) == 42
