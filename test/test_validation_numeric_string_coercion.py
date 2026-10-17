"""Defensive repair for an integer argument that reached a string-typed field as
a NUMBER, applied at the MCP tool-call ENTRY POINTS only.

Some agent runtimes defer a tool's schema and, when they later marshal the
model's ``arguments``, re-type a top-level argument whose value looks like a
number into a JSON number — even when the loaded schema declared that field a
string. The repair converts that ``int`` back to its string form (``str(42) ==
"42"`` is exact) so a value the author meant as a string validates as one.

It lives in ``coerce_mcp_tool_args`` (the ``ToolSchema`` path used by
``mcp_core`` and the computer-use server) and ``coerce_mcp_tool_args_json_schema``
(the JSON-Schema path used by the mochi server), NOT in the shared
``validate_field``: the dashboard HTTP endpoints validate through that function
and never see the re-typing, so coercing there would silently change their
contract. A ``float`` is left to fail loudly (``str(float)`` is lossy relative
to the author's text), a ``bool`` falls through to the type error, and an
``int`` beyond ``2**53`` is REFUSED rather than coerced — the same
precision-loss hazard the repair declines to paper over for floats.
"""

from __future__ import annotations

import pytest

from kiro_crew.validation import (
    FieldSpec,
    ToolSchema,
    ValidationError,
    coerce_mcp_tool_args,
    coerce_mcp_tool_args_json_schema,
    validate_field,
    validate_tool_args,
)

# A string-only field (the shape the deferred-tool bug mangles).
_STR_SPEC = FieldSpec("reply_to", str, max_len=64)
_SCHEMA = ToolSchema(tool_name="post_message", fields=[FieldSpec("thread_id", str, max_len=64)])


# ── The repair now lives ONLY at the entry point, not in validate_field ──


def test_validate_field_no_longer_coerces_an_int_on_a_string_field() -> None:
    """The shared validator keeps its original contract: an int on a string
    field is a plain type error (so the ~17 dashboard HTTP endpoints that go
    through it are unchanged)."""
    with pytest.raises(ValidationError):
        validate_field(42, _STR_SPEC)
    with pytest.raises(ValidationError):
        validate_tool_args({"thread_id": 42}, _SCHEMA)


# ── ToolSchema entry-point coercion (mcp_core / computer-use) ──


@pytest.mark.parametrize(
    "number, expected",
    [
        (42, "42"),
        (0, "0"),
        (-7, "-7"),
        # Large ids round-trip exactly as long as they stay within 2**53.
        (9007199254740992, "9007199254740992"),  # exactly 2**53 is allowed
    ],
)
def test_coerce_mcp_tool_args_converts_int_on_string_field(number: int, expected: str) -> None:
    repaired = coerce_mcp_tool_args({"thread_id": number}, _SCHEMA)
    assert repaired["thread_id"] == expected
    assert isinstance(repaired["thread_id"], str)
    # And the coerced dict validates cleanly end to end.
    cleaned = validate_tool_args(repaired, _SCHEMA)
    assert cleaned["thread_id"] == expected


def test_coerce_then_validate_runs_the_string_rules() -> None:
    """The repaired string is validated like any other: max_len still applies."""
    schema = ToolSchema(tool_name="t", fields=[FieldSpec("code", str, max_len=4)])
    assert (
        validate_tool_args(coerce_mcp_tool_args({"code": 1234}, schema), schema)["code"] == "1234"
    )
    with pytest.raises(ValidationError):
        validate_tool_args(coerce_mcp_tool_args({"code": 12345}, schema), schema)


# ── 2**53 refusal: a too-large int cannot be a precise string id ──


@pytest.mark.parametrize("number", [2**53 + 1, -(2**53) - 1, 10**30, -(10**30)])
def test_int_beyond_exact_bound_is_refused_toolschema(number: int) -> None:
    with pytest.raises(ValidationError):
        coerce_mcp_tool_args({"thread_id": number}, _SCHEMA)


# ── A FLOAT and a BOOL are not coerced ──


@pytest.mark.parametrize("number", [1790284307.156629, 1790284307.15662, 3.5, 3.0, 1000.0])
def test_float_on_a_string_field_is_left_for_the_type_error(number: float) -> None:
    """A float is passed through unchanged by the coercion, then rejected by
    validation — a loud ``expected str`` rather than a silently-wrong value."""
    repaired = coerce_mcp_tool_args({"thread_id": number}, _SCHEMA)
    assert repaired["thread_id"] == number  # untouched
    with pytest.raises(ValidationError):
        validate_tool_args(repaired, _SCHEMA)


def test_bool_on_a_string_field_is_left_for_the_type_error() -> None:
    """bool is an int subclass but is NOT a numeric id; it must not become
    ``"True"``/``"False"``."""
    for value in (True, False):
        repaired = coerce_mcp_tool_args({"thread_id": value}, _SCHEMA)
        assert repaired["thread_id"] is value
        with pytest.raises(ValidationError):
            validate_tool_args(repaired, _SCHEMA)


# ── Untouched cases ──


def test_real_string_on_string_field_is_unchanged() -> None:
    assert coerce_mcp_tool_args({"thread_id": "hello"}, _SCHEMA)["thread_id"] == "hello"
    assert (
        coerce_mcp_tool_args({"thread_id": "1790284307.156629"}, _SCHEMA)["thread_id"]
        == "1790284307.156629"
    )


def test_number_on_a_numeric_field_is_left_a_number() -> None:
    """A field that legitimately wants a number must keep getting one."""
    schema = ToolSchema(
        tool_name="t",
        fields=[FieldSpec("count", int, min_val=0, max_val=1000), FieldSpec("mixed", (str, int))],
    )
    repaired = coerce_mcp_tool_args({"count": 42, "mixed": 7}, schema)
    assert repaired["count"] == 42 and isinstance(repaired["count"], int)
    # A field accepting both str and int is not string-only; the int stays.
    assert repaired["mixed"] == 7 and isinstance(repaired["mixed"], int)


def test_unknown_argument_is_passed_through_untouched() -> None:
    """An argument the schema does not name is left alone (validate_tool_args
    still rejects it later as unknown)."""
    repaired = coerce_mcp_tool_args({"thread_id": 42, "extra": 99}, _SCHEMA)
    assert repaired["extra"] == 99


# ── JSON-Schema entry-point coercion (mochi) ──

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "thread_id": {"type": "string"},
        "count": {"type": "integer"},
    },
}


def test_json_schema_coercion_converts_int_on_string_property() -> None:
    repaired = coerce_mcp_tool_args_json_schema({"thread_id": 42}, _JSON_SCHEMA)
    assert repaired["thread_id"] == "42"
    assert isinstance(repaired["thread_id"], str)


def test_json_schema_coercion_leaves_a_numeric_property_a_number() -> None:
    repaired = coerce_mcp_tool_args_json_schema({"count": 42}, _JSON_SCHEMA)
    assert repaired["count"] == 42
    assert isinstance(repaired["count"], int)


def test_json_schema_coercion_leaves_bool_and_string_alone() -> None:
    repaired = coerce_mcp_tool_args_json_schema({"thread_id": "hi"}, _JSON_SCHEMA)
    assert repaired["thread_id"] == "hi"
    repaired = coerce_mcp_tool_args_json_schema({"thread_id": True}, _JSON_SCHEMA)
    assert repaired["thread_id"] is True


@pytest.mark.parametrize("number", [2**53 + 1, -(2**53) - 1, 10**30])
def test_json_schema_coercion_refuses_int_beyond_bound(number: int) -> None:
    with pytest.raises(ValidationError):
        coerce_mcp_tool_args_json_schema({"thread_id": number}, _JSON_SCHEMA)


def test_json_schema_coercion_no_properties_is_a_noop() -> None:
    assert coerce_mcp_tool_args_json_schema({"x": 1}, {"type": "object"}) == {"x": 1}
    assert coerce_mcp_tool_args_json_schema({"x": 1}, True) == {"x": 1}
