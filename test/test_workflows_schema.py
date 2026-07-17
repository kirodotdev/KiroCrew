"""GATES C1–C3 — structured output for ctx.agent(schema=).

C1: a valid object is parsed + validated + returned.
C2: malformed/invalid output triggers bounded retry, then returns None after N fails.
C3: an object that violates the JSON Schema is rejected (not returned).

Exercises both the pure validator (``schema.py``) and the schema= path THROUGH the
runner, using a stub text-producer that returns canned (malformed then valid) JSON
— never a real agent. See ``docs/dynamic-workflows/GATES.md`` (C1–C3).
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
