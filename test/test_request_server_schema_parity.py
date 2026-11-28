"""``request_server``'s argument validator accepts every field its inputSchema advertises.

The advertised schema (``mcp_tools.catalog.server_request``) and the validator
(``REQUEST_SERVER_SCHEMA``) are two definitions of one tool's arguments; a field
only the first declares reaches the agent as an offer the validator then refuses.
"""

from __future__ import annotations

import pytest

from kiro_crew.mcp_tools.catalog.server_request import schemas
from kiro_crew.validation import REQUEST_SERVER_SCHEMA, ValidationError, validate_tool_args

_INPUT_SCHEMA = schemas()[0]["inputSchema"]


def test_the_validator_declares_exactly_the_advertised_properties():
    assert {s.name for s in REQUEST_SERVER_SCHEMA.fields} == set(_INPUT_SCHEMA["properties"])


def test_the_validator_requires_exactly_the_advertised_required_fields():
    required = {s.name for s in REQUEST_SERVER_SCHEMA.fields if s.required}
    assert required == set(_INPUT_SCHEMA["required"])


def test_each_advertised_enum_matches_the_validator_allow_list():
    by_name = {s.name: s for s in REQUEST_SERVER_SCHEMA.fields}
    for name, prop in _INPUT_SCHEMA["properties"].items():
        if "enum" in prop and by_name[name].allowed is not None:
            assert by_name[name].allowed == frozenset(prop["enum"]), name


@pytest.mark.parametrize(
    "args",
    [
        {
            "server": "demo",
            "source": "custom",
            "entry": {"command": "node", "args": ["server.js"]},
            "uses_chat_folder": False,
            "target": "kiro-cli",
        },
        {"server": "demo", "action": "remove"},
        {
            "server": "demo",
            "action": "change",
            "entry": {"url": "https://x"},
            "uses_chat_folder": True,
        },
        {"server": "fs", "source": "gateway", "reason": "needs files"},
    ],
)
def test_an_advertised_call_validates(args):
    assert validate_tool_args(dict(args), REQUEST_SERVER_SCHEMA) == args


@pytest.mark.parametrize(
    "args, field",
    [
        ({"server": "demo", "action": "rename"}, "action"),
        ({"server": "demo", "source": "custom", "entry": "node server.js"}, "entry"),
        ({"server": "demo", "source": "custom", "uses_chat_folder": "no"}, "uses_chat_folder"),
        ({"server": "demo", "source": "custom", "target": "everywhere"}, "target"),
        ({"server": "demo", "session": "x"}, "session"),
    ],
)
def test_a_malformed_call_is_refused_on_its_field(args, field):
    with pytest.raises(ValidationError) as exc:
        validate_tool_args(args, REQUEST_SERVER_SCHEMA)
    assert exc.value.field == field
