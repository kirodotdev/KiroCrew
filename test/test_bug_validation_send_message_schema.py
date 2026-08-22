"""RED test for SEND_MESSAGE_SCHEMA caller_session pattern defect.

The caller_session FieldSpec in SEND_MESSAGE_SCHEMA must accept the same
two-segment ``cron:<job_id>:<run_id>`` form that CRON_SESSION_RE documents and
accepts. The buggy inline pattern ``^(cron:[a-zA-Z0-9]+)?$`` only matches the
single-segment ``cron:<job_id>`` form, so a legitimate two-segment caller_session
is wrongly rejected.

Also pins the *advertise/validate parity* half of the same defect class: the
schema this file guards is the one ``mcp_core._validate_args`` enforces, and
``validate_tool_args`` REJECTS an unknown field outright — so a property the
``send_message`` descriptor advertises to the model but the schema does not
declare is a tool argument that can never be passed. Nothing at runtime notices,
because the two halves live in different modules.
"""

from __future__ import annotations

import pytest

from kiro_crew.validation import (
    CRON_SESSION_RE,
    SEND_MESSAGE_SCHEMA,
    ValidationError,
    validate_field,
    validate_tool_args,
)


def _caller_session_spec():
    for spec in SEND_MESSAGE_SCHEMA.fields:
        if spec.name == "caller_session":
            return spec
    raise AssertionError("caller_session field not found in SEND_MESSAGE_SCHEMA")


def test_agent_defect():
    value = "cron:job1:run2"

    # CRON_SESSION_RE is the documented authority and accepts the two-segment form.
    assert CRON_SESSION_RE.match(value), "CRON_SESSION_RE should accept cron:<job_id>:<run_id>"

    spec = _caller_session_spec()
    # The schema's caller_session field must agree: validation must not reject it.
    try:
        cleaned = validate_field(value, spec)
    except ValidationError as exc:
        pytest.fail(
            f"caller_session pattern rejected documented two-segment form {value!r}: {exc}"
        )
    assert cleaned == value


def _send_message_descriptor():
    from kiro_crew.mcp_tools.messaging import schemas

    for tool in schemas():
        if tool["name"] == "send_message":
            return tool
    raise AssertionError("send_message descriptor not found in mcp_tools.messaging.schemas()")


def test_advertised_properties_are_all_accepted_by_the_schema():
    """Every property the descriptor advertises must be a declared FieldSpec.

    ``validate_tool_args`` raises on an unknown field, so an advertised-but-
    undeclared property is a tool argument the model is told to use and is then
    refused for using. ``channel_type`` is the one this arrived with; the check is
    written over the whole property map so the next one cannot repeat it.
    """
    advertised = set(_send_message_descriptor()["inputSchema"]["properties"])
    declared = {spec.name for spec in SEND_MESSAGE_SCHEMA.fields}
    assert advertised <= declared, (
        "advertised but not declared in SEND_MESSAGE_SCHEMA (validate_tool_args "
        f"will reject these): {sorted(advertised - declared)}"
    )


def test_channel_type_is_advertised_and_validates():
    """channel_type is the non-Slack routing field, and a real transport name
    survives the schema gate that mcp_core._validate_args applies."""
    assert "channel_type" in _send_message_descriptor()["inputSchema"]["properties"]
    cleaned = validate_tool_args({"text": "hi", "channel_type": "telegram"}, SEND_MESSAGE_SCHEMA)
    assert cleaned["channel_type"] == "telegram"


@pytest.mark.parametrize("value", ["Telegram", "telegram:99887766", "tele gram", "telegram1"])
def test_channel_type_rejects_non_transport_shapes(value):
    """Shape gate only — the authoritative closed set lives in the handler — but
    it must still refuse anything that is not a bare lowercase transport name, so
    a session key or a namespaced value cannot arrive as a transport."""
    with pytest.raises(ValidationError):
        validate_tool_args({"text": "hi", "channel_type": value}, SEND_MESSAGE_SCHEMA)
