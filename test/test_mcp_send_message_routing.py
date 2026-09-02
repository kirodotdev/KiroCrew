"""The MCP ``send_message`` tool's advertised shape must match what validates.

``validate_tool_args`` REJECTS an unknown field, so a property advertised in the
tool's ``inputSchema`` but missing from ``SEND_MESSAGE_SCHEMA`` does not merely go
unvalidated — the whole call fails and the capability is 0% reachable over MCP,
while the dashboard path that shares the handler keeps working. That asymmetry is
invisible to every test that exercises only one side, which is why the agreement
itself is pinned here.
"""

from __future__ import annotations

import pytest

from kiro_crew.mcp_tools.messaging import schemas
from kiro_crew.validation import SEND_MESSAGE_SCHEMA, ValidationError, validate_tool_args


def _advertised() -> dict:
    for tool in schemas():
        if tool["name"] == "send_message":
            return tool["inputSchema"]["properties"]
    raise AssertionError("send_message is not advertised")


def test_every_advertised_property_is_accepted_by_the_validator() -> None:
    known = {spec.name for spec in SEND_MESSAGE_SCHEMA.fields}
    missing = sorted(set(_advertised()) - known)

    assert not missing, (
        f"advertised but unvalidatable, so every call carrying one fails: {missing}. "
        "Add a FieldSpec to SEND_MESSAGE_SCHEMA in the same change."
    )


def test_the_routing_pair_validates() -> None:
    cleaned = validate_tool_args(
        {"text": "hi", "channel_type": "webex", "target_id": "user:a@b.com"},
        SEND_MESSAGE_SCHEMA,
    )

    assert cleaned["channel_type"] == "webex"
    assert cleaned["target_id"] == "user:a@b.com"


def test_a_target_id_without_a_channel_type_is_refused() -> None:
    """A destination id with no transport to resolve it against is under-specified.

    Ignoring the lone field would fall back to the default Slack/dashboard
    destination — delivering the message somewhere the caller did not name.
    """
    with pytest.raises(ValidationError):
        validate_tool_args({"text": "hi", "target_id": "user:a@b.com"}, SEND_MESSAGE_SCHEMA)


def test_a_channel_type_alone_is_complete() -> None:
    """``channel_type`` on its own is NOT half a pair.

    It names the non-Slack conversation this session already belongs to; adding
    ``target_id`` narrows that transport to one explicit configured destination on
    it. So only the reverse is under-specified.
    """
    cleaned = validate_tool_args({"text": "hi", "channel_type": "webex"}, SEND_MESSAGE_SCHEMA)

    assert cleaned["channel_type"] == "webex"
    assert "target_id" not in cleaned or not cleaned["target_id"]


@pytest.mark.parametrize(
    "channel_type",
    ["WEBEX", "we bex", "1webex", "webex!", "x" * 40, "../etc", ""],
)
def test_a_channel_type_that_is_not_a_channel_name_shape_is_refused(channel_type: str) -> None:
    with pytest.raises(ValidationError):
        validate_tool_args(
            {"text": "hi", "channel_type": channel_type, "target_id": "x"},
            SEND_MESSAGE_SCHEMA,
        )


@pytest.mark.parametrize("target_id", ["with space", "line\nbreak", "tab\there", "x" * 513])
def test_a_target_id_with_control_characters_or_over_length_is_refused(target_id: str) -> None:
    # The id is opaque and channel-defined, so this bounds length and excludes
    # whitespace/control characters rather than pretending to know the grammar.
    with pytest.raises(ValidationError):
        validate_tool_args(
            {"text": "hi", "channel_type": "webex", "target_id": target_id},
            SEND_MESSAGE_SCHEMA,
        )


def test_an_opaque_base64_room_id_is_accepted() -> None:
    # A real Webex room id is a ~90-char base64 Hydra blob.
    room = "Y2lzY29zcGFyazovL3VzL1JPT00vZXhhbXBsZS1yb29tLWlkZW50aWZpZXItdGhhdC1pcy1sb25n"
    cleaned = validate_tool_args(
        {"text": "hi", "channel_type": "webex", "target_id": f"room:{room}"},
        SEND_MESSAGE_SCHEMA,
    )

    assert cleaned["target_id"].endswith(room)


def test_a_plain_send_still_validates_without_the_pair() -> None:
    # The fields are additive: a caller that never routes is unaffected.
    assert validate_tool_args({"text": "hi"}, SEND_MESSAGE_SCHEMA)["text"] == "hi"
