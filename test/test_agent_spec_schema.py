"""The agent spec field table, the offline checker and the JSON Schema built from it.

Pins that one table (``FIELDS``) drives both ``check_spec`` and ``json_schema``, that
each class of mistake is reported at the field path the author must edit, and that a
healthy spec (including the shapes Kiro Crew itself writes) reports nothing.
"""

from __future__ import annotations

from kiro_crew import agent_spec_schema as schema


def _problems(spec, **kwargs):
    return dict(schema.check_spec(spec, **kwargs))


def test_a_healthy_spec_reports_nothing() -> None:
    spec = {
        "name": "reviewer",
        "description": "Reviews code",
        "prompt": "file://./reviewer.md",
        "model": "claude-sonnet-4",
        "mcpServers": {
            "git": {"command": "git-mcp", "args": [], "env": {"A": "1"}},
            "remote": {"url": "https://example.com/mcp"},
            "governed": {"type": "registry"},
        },
        "tools": ["read", "@git", "@remote/fetch", "@builtin", "*"],
        "allowedTools": ["read", "@git/status"],
        "resources": ["file://README.md", {"type": "knowledgeBase", "name": "kb"}],
        "hooks": {"agentSpawn": [{"command": "git status"}]},
        "includeMcpJson": False,
    }
    servers = {"git", "remote", "governed"}
    assert schema.check_spec(spec, configured_servers=servers) == []


def test_an_unknown_top_level_field_is_reported_unless_allowed() -> None:
    spec = {"name": "a", "colour": "red"}
    assert _problems(spec) == {"colour": "unknown field"}
    assert schema.check_spec(spec, allow_unknown=True) == []


def test_a_value_of_the_wrong_shape_names_its_field() -> None:
    spec = {
        "description": 5,
        "includeMcpJson": "yes",
        "mcpServers": [],
        "tools": "read",
        "allowedTools": ["read", 3],
        "resources": [1],
        "hooks": "echo",
    }
    problems = _problems(spec)
    assert problems["description"] == "must be a string"
    assert problems["includeMcpJson"] == "must be true or false"
    assert problems["mcpServers"] == "must be an object"
    assert problems["tools"] == "must be a list of strings"
    assert problems["allowedTools"] == "entry 1 must be a string"
    assert problems["resources"] == "entry 0 must be a string or an object"
    assert problems["hooks"] == "must be an object or a list"


def test_a_spec_that_is_not_an_object_is_one_problem() -> None:
    assert schema.check_spec(["name"]) == [("", "the spec must be an object")]


def test_malformed_tool_entries_are_reported_by_position() -> None:
    spec = {"tools": ["read", "", "@", "@git/", "two words"], "allowedTools": ["@/x"]}
    problems = schema.check_spec(spec)
    assert ("tools[1]", "'' is empty") in problems
    assert ("tools[2]", "'@' names no server after @") in problems
    assert ("tools[3]", "'@git/' names no tool after the /") in problems
    assert ("tools[4]", "'two words' contains whitespace") in problems
    assert ("allowedTools[0]", "'@/x' names no server after @") in problems


def test_a_server_ref_nothing_defines_is_reported_per_field() -> None:
    spec = {"tools": ["@ghost", "@git/status", "@builtin"], "allowedTools": ["@ghost/run"]}
    problems = schema.check_spec(spec, configured_servers={"git"})
    assert ("tools", "names @ghost, which no server this spec can reach defines") in problems
    assert ("allowedTools", "names @ghost, which no server this spec can reach defines") in problems
    assert len(problems) == 2


def test_the_server_ref_check_is_skipped_without_a_configured_set() -> None:
    assert schema.check_spec({"tools": ["@ghost"]}) == []


def test_a_server_entry_that_cannot_launch_is_reported() -> None:
    spec = {"mcpServers": {"empty": {}, "bad": "git-mcp", "typed": {"command": 5, "args": "x"}}}
    problems = _problems(spec)
    assert problems["mcpServers.empty"] == "names neither a command to run nor a url to reach"
    assert problems["mcpServers.bad"] == "must be an object"
    assert problems["mcpServers.typed.command"] == "must be a string"
    assert problems["mcpServers.typed.args"] == "must be a list of strings"


def test_the_schema_is_built_from_the_same_table() -> None:
    built = schema.json_schema()
    assert set(built["properties"]) == {f.name for f in schema.FIELDS}
    assert built["additionalProperties"] is False
    assert built["properties"]["allowedTools"]["items"] == {"type": "string"}
    assert all(f.kind in schema.KINDS for f in schema.FIELDS)
    assert all(built["properties"][f.name]["description"] == f.doc for f in schema.FIELDS)


def test_every_spec_the_schema_calls_valid_the_checker_accepts() -> None:
    # The two readings cannot drift: a field in one is in the other.
    for field in schema.FIELDS:
        assert schema.check_spec({field.name: _sample(field.kind)}) == [], field.name


def _sample(kind: str):
    return {
        "string": "x",
        "bool": True,
        "object": {},
        "string_list": ["read"],
        "resources": ["file://x"],
        "hooks": {},
        "any": {"anything": 1},
    }[kind]
