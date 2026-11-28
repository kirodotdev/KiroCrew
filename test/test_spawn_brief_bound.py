"""A sub-agent brief is held to the task-spec bound, and the tool list advertises it."""

from __future__ import annotations

import re

import pytest

from kiro_crew.mcp_tools.spawn_parts.schemas import schemas
from kiro_crew.validation import (
    MAX_LONG_STRING,
    SPAWN_CONTINUE_SCHEMA,
    SPAWN_RUN_SCHEMA,
    SPAWN_STEER_SCHEMA,
    ValidationError,
    validate_tool_args,
)
from kiro_crew.validation_parts.json_schema_args import validate_mcp_tool_arguments

_BRIEF_CALLS = [
    (SPAWN_RUN_SCHEMA, lambda text: {"task": text}, "task"),
    (SPAWN_RUN_SCHEMA, lambda text: {"tasks": [{"task": text}]}, "tasks[0].task"),
    (SPAWN_RUN_SCHEMA, lambda text: {"tasks": [text]}, "tasks"),
    (SPAWN_CONTINUE_SCHEMA, lambda text: {"conversation": "c1", "task": text}, "task"),
    (SPAWN_STEER_SCHEMA, lambda text: {"agent_id": "a1", "message": text}, "message"),
]


@pytest.mark.parametrize(("schema", "build", "field"), _BRIEF_CALLS)
def test_a_brief_past_the_short_text_bound_is_accepted(schema, build, field) -> None:
    validate_tool_args(build("x" * 12_000), schema)


@pytest.mark.parametrize(("schema", "build", "field"), _BRIEF_CALLS)
def test_a_brief_over_the_task_spec_bound_is_refused_naming_the_field(schema, build, field) -> None:
    with pytest.raises(ValidationError, match=rf"{re.escape(field)}.*{MAX_LONG_STRING}"):
        validate_tool_args(build("x" * (MAX_LONG_STRING + 1)), schema)


def _props(tool: str) -> dict:
    spec = next(s for s in schemas() if s["name"] == tool)
    return spec["inputSchema"]["properties"]


@pytest.mark.parametrize(
    ("tool", "path"),
    [
        ("spawn_run", ("task",)),
        ("spawn_run", ("tasks", "items")),
        ("spawn_run", ("tasks", "items", "properties", "task")),
        ("spawn_continue", ("task",)),
        ("spawn_steer", ("message",)),
    ],
)
def test_every_brief_field_advertises_the_bound_it_is_held_to(tool, path) -> None:
    node = _props(tool)
    for key in path:
        node = node[key]
    assert node["maxLength"] == MAX_LONG_STRING


@pytest.mark.parametrize(
    ("tool", "path"),
    [
        ("spawn_run", ("task",)),
        ("spawn_run", ("tasks", "items", "properties", "task")),
        ("spawn_continue", ("task",)),
        ("spawn_steer", ("message",)),
    ],
)
def test_the_brief_description_names_the_limit(tool, path) -> None:
    node = _props(tool)
    for key in path:
        node = node[key]
    assert f"HARD LIMIT: {MAX_LONG_STRING} characters" in node["description"]


@pytest.mark.parametrize(
    ("arguments", "refused"),
    [
        ({"tasks": ["x" * (MAX_LONG_STRING + 1)]}, True),
        ({"tasks": [{"task": "x" * (MAX_LONG_STRING + 1)}]}, True),
        ({"tasks": ["x" * 12_000, {"task": "x" * 12_000}]}, False),
    ],
)
def test_the_advertised_tasks_schema_binds_both_item_forms(arguments, refused) -> None:
    schema = next(s for s in schemas() if s["name"] == "spawn_run")["inputSchema"]
    if refused:
        with pytest.raises(ValidationError, match="maxLength"):
            validate_mcp_tool_arguments(arguments, schema)
    else:
        validate_mcp_tool_arguments(arguments, schema)
