"""GATES C1–C3 — structured output for ctx.agent(schema=).

C1: a valid object is parsed + validated + returned.
C2: malformed/invalid output triggers bounded retry, then returns None after N fails.
C3: an object that violates the JSON Schema is rejected (not returned).

Exercises both the pure validator (``schema.py``) and the schema= path THROUGH the
runner, using a stub text-producer that returns canned (malformed then valid) JSON
— never a real agent. See ``docs/system-specs/modules/workflow-gates.md`` (C1–C3).
"""

from __future__ import annotations

import pytest

from kiro_crew.workflows.runner import WorkflowRunner
from kiro_crew.workflows.schema import (
    coerce_and_validate,
    parse_json,
    run_with_schema,
    validate_against_schema,
)

# NB: only the async tests carry @pytest.mark.asyncio (not a module-global mark),
# so the sync validator tests don't get spuriously marked async.
NOW = "2026-06-18T00:00:00Z"

FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "severity": {"type": "string", "enum": ["low", "high"]},
        "count": {"type": "integer"},
    },
    "required": ["title", "severity"],
}


# --------------------------------------------------------------------------- #
# Pure validator (schema.py)
# --------------------------------------------------------------------------- #


def test_c1_valid_object_passes() -> None:
    assert validate_against_schema({"title": "x", "severity": "low"}, FINDING_SCHEMA) == []


def test_c3_missing_required_rejected() -> None:
    errs = validate_against_schema({"title": "x"}, FINDING_SCHEMA)
    assert any("required" in e and "severity" in e for e in errs)


def test_c3_wrong_type_rejected() -> None:
    errs = validate_against_schema({"title": 5, "severity": "low"}, FINDING_SCHEMA)
    assert any("title" in e and "type" in e for e in errs)


def test_c3_enum_violation_rejected() -> None:
    errs = validate_against_schema({"title": "x", "severity": "URGENT"}, FINDING_SCHEMA)
    assert any("enum" in e for e in errs)


def test_c3_bool_is_not_integer() -> None:
    errs = validate_against_schema({"title": "x", "severity": "low", "count": True}, FINDING_SCHEMA)
    assert any("count" in e for e in errs)


def test_union_type_still_enforces_required_and_properties() -> None:
    # GPT review round 2: keying the object sub-checks on the literal string
    # "object" skipped required/properties for union spellings, so an invalid
    # object validated under ["object", "null"].
    union = dict(FINDING_SCHEMA, type=["object", "null"])
    assert any("required" in e for e in validate_against_schema({}, union))
    assert validate_against_schema(None, union) == []
    assert validate_against_schema({"title": "x", "severity": "low"}, union) == []


def test_union_type_still_enforces_array_items() -> None:
    schema = {"type": ["array", "null"], "items": {"type": "integer"}}
    assert validate_against_schema([1, "two"], schema) != []
    assert validate_against_schema([1, 2], schema) == []
    assert validate_against_schema(None, schema) == []


def test_coerce_union_preferred_object_is_still_schema_checked() -> None:
    # The full round-2 chain: prefer steers the {} candidate in, and the
    # validator must reject it (missing required) so the retry loop re-asks —
    # not return an invalid object as validated.
    union = dict(FINDING_SCHEMA, type=["object", "null"])
    value, errs = coerce_and_validate("note [1] then {}", union)
    assert value is None and errs


def test_nested_array_items_validated() -> None:
    schema = {
        "type": "object",
        "properties": {"xs": {"type": "array", "items": {"type": "integer"}}},
    }
    assert validate_against_schema({"xs": [1, 2, 3]}, schema) == []
    assert validate_against_schema({"xs": [1, "two"]}, schema) != []


# --------------------------------------------------------------------------- #
# JSON parsing tolerance
# --------------------------------------------------------------------------- #


def test_parse_plain_json() -> None:
    assert parse_json('{"a": 1}') == {"a": 1}


def test_parse_fenced_json() -> None:
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_with_surrounding_prose() -> None:
    assert parse_json('Here you go: {"a": 1} hope that helps') == {"a": 1}


def test_parse_non_json_raises() -> None:
    with pytest.raises(ValueError):
        parse_json("not json at all")


def test_parse_json_nesting_bomb_keeps_valueerror_contract() -> None:
    # Adversarially deep nesting must surface as the contract ValueError, not a
    # RecursionError escaping the shared scanner (pre-push review finding). The
    # balanced form overflowed even the old span fallback; both are contained now.
    with pytest.raises(ValueError):
        parse_json("x " + "[" * 100_000)
    with pytest.raises(ValueError):
        parse_json("x " + "[" * 100_000 + "]" * 100_000)


def test_nesting_bomb_cannot_launder_a_candidate_past_refusal() -> None:
    # GPT review round 4: a bomb between a worked example and the real payload
    # truncated the scan after collecting only the example, defeating the
    # ambiguity refusal. The scan now fails closed on overflow: nothing is
    # returned, parsing errors, and the retry loop re-asks.
    text = (
        'Example: {"title": "example", "severity": "low"} '
        + "[" * 100_000
        + ' final: {"title": "real", "severity": "high"}'
    )
    value, errs = coerce_and_validate(text, FINDING_SCHEMA)
    assert value is None and errs


def test_parse_scalar_strict_path_preserved() -> None:
    # Whole-text scalars parse via the strict path (prose recovery only
    # decodes at container delimiters).
    assert parse_json("42") == 42
    assert parse_json("```json\ntrue\n```") is True


def test_parse_json_stray_brace_in_preamble() -> None:
    # The old outermost-span (find/rfind) fallback corrupted the span on any
    # stray brace in prose ('{placeholder} … 1}' is not valid JSON) and raised.
    # The shared scanner skips the failed offset and recovers the real object.
    assert parse_json('use {placeholder} syntax: {"a": 1}') == {"a": 1}


def test_parse_json_stray_brace_after_payload() -> None:
    # find/rfind spanned '{"a": 1} and note {this}' — unparseable — and raised.
    assert parse_json('{"a": 1} and note {this}') == {"a": 1}


def test_parse_prose_array_yields_outer_array_not_inner_object() -> None:
    # Preserved guarantee from the old fallback's delimiter ordering.
    assert parse_json('Result: [{"a": 1}] done') == [{"a": 1}]


def test_coerce_prefers_schema_shape_object_over_leading_array() -> None:
    # Stray '[1, 2]' in prose parses as an array and appears FIRST; the object
    # schema's prefer predicate selects the object payload anyway. (The old
    # span fallback returned [1, 2] here and validation failed.)
    text = 'steps [1, 2] then: {"title": "x", "severity": "high"}'
    value, errs = coerce_and_validate(text, FINDING_SCHEMA)
    assert errs == [] and value == {"title": "x", "severity": "high"}


def test_coerce_prefers_schema_shape_array_over_leading_object() -> None:
    schema = {"type": "array", "items": {"type": "integer"}}
    text = 'config {"note": "x"} follows: [1, 2, 3]'
    value, errs = coerce_and_validate(text, schema)
    assert errs == [] and value == [1, 2, 3]


def test_coerce_restated_identical_payload_is_not_ambiguous() -> None:
    # A model restating the same payload twice is not ambiguity (shared
    # extractor contract: equal preferred matches collapse to one).
    payload = '{"title": "x", "severity": "low"}'
    text = f"{payload}\nAs stated above: {payload}"
    value, errs = coerce_and_validate(text, FINDING_SCHEMA)
    assert errs == [] and value == {"title": "x", "severity": "low"}


def test_coerce_two_different_shaped_candidates_refuse_to_guess() -> None:
    # Two DIFFERENT object candidates under an object schema: the extractor
    # refuses to guess, parsing fails, and the retry loop re-asks (returning
    # either one would risk validating a worked example as the payload).
    text = '{"title": "a", "severity": "low"} or {"title": "b", "severity": "high"}'
    value, errs = coerce_and_validate(text, FINDING_SCHEMA)
    assert value is None and errs


def test_coerce_nullable_union_type_keeps_ambiguity_refusal() -> None:
    # GPT review round 1: `type: ["object", "null"]` must derive the object
    # preference — routing unions to no-preference let a worked example win
    # first-match and validate as the payload instead of triggering refusal.
    schema = dict(FINDING_SCHEMA, type=["object", "null"])
    text = (
        'Example: {"title": "example", "severity": "low"} ... '
        'final: {"title": "real", "severity": "high"}'
    )
    value, errs = coerce_and_validate(text, schema)
    assert value is None and errs


def test_coerce_nullable_array_union_prefers_array() -> None:
    schema = {"type": ["array", "null"], "items": {"type": "integer"}}
    value, errs = coerce_and_validate('note {"a": 1} then: [1, 2]', schema)
    assert errs == [] and value == [1, 2]


def test_coerce_dual_container_union_has_no_single_preference() -> None:
    # A union admitting BOTH containers accepts either shape, so recovery
    # keeps first-match fallback semantics.
    schema = {"type": ["object", "array"]}
    value, errs = coerce_and_validate('pick [1, 2] or {"a": 1}', schema)
    assert errs == [] and value == [1, 2]


def test_coerce_and_validate_round() -> None:
    value, errs = coerce_and_validate('{"title": "x", "severity": "high"}', FINDING_SCHEMA)
    assert errs == [] and value == {"title": "x", "severity": "high"}


# --------------------------------------------------------------------------- #
# C2 — bounded retry loop
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_c2_retry_then_success() -> None:
    calls = {"n": 0}

    async def produce(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "garbage, not json"  # first attempt malformed
        return '{"title": "ok", "severity": "low"}'  # retry valid

    out = await run_with_schema(produce, "find a thing", FINDING_SCHEMA, retries=2)
    assert out == {"title": "ok", "severity": "low"}
    assert calls["n"] == 2  # one retry was needed


@pytest.mark.asyncio
async def test_c2_all_malformed_returns_none() -> None:
    calls = {"n": 0}

    async def produce(prompt: str) -> str:
        calls["n"] += 1
        return "still not json"

    out = await run_with_schema(produce, "find a thing", FINDING_SCHEMA, retries=2)
    assert out is None
    assert calls["n"] == 3  # initial + 2 retries, then give up


@pytest.mark.asyncio
async def test_c2_schema_violation_retried_then_none() -> None:
    async def produce(prompt: str) -> str:
        return '{"title": "x"}'  # always missing required 'severity'

    out = await run_with_schema(produce, "x", FINDING_SCHEMA, retries=1)
    assert out is None


# --------------------------------------------------------------------------- #
# schema= THROUGH the runner (integration)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_schema_path_returns_validated_dict_through_runner() -> None:
    async def agent_fn(prompt: str, opts: dict):
        # The runner must route schema= through run_with_schema; emulate a model
        # that returns valid JSON for the (schema-augmented) prompt.
        if opts.get("schema"):
            return '{"title": "bug", "severity": "high"}'
        return "plain text"

    script = (
        'META = {"name": "s"}\n'
        "async def workflow(ctx):\n"
        "    SCH = {'type': 'object', 'properties': {'title': {'type': 'string'},"
        " 'severity': {'type': 'string'}}, 'required': ['title', 'severity']}\n"
        "    return await ctx.agent('find', schema=SCH)\n"
    )
    res = await WorkflowRunner(agent_fn=agent_fn).run(script, run_id="wf_s", now=NOW)
    assert res.ok, res.error
    assert res.result == {"title": "bug", "severity": "high"}
