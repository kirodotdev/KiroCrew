"""`session_send`'s tool layer: what it forwards, and what it reports back.

Three delivery outcomes reach this layer from the API (`steered`, `started`,
neither), and a caller coordinating several sessions acts on the difference. A
steer that quietly fell back to the queue while the report says "queued" reads as
"the target was busy" — true, but not the thing that happened — so each outcome is
asserted on its own.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.mcp_dashboard import _call_tool_inner
from kiro_crew.validation import SESSION_SEND_SCHEMA, ValidationError, validate_tool_args


@pytest.fixture(autouse=True)
def _caller():
    with patch(
        "kiro_crew.mcp_core._resolve_session_key_strict",
        return_value="dashboard:chat-1-100",
    ):
        yield


def _send(args: dict, resp: dict):
    with patch("kiro_crew.mcp_dashboard._post", return_value=resp) as mock_post:
        out = _call_tool_inner("session_send", args)
    return out, mock_post


class TestSteerForwarding:
    def test_steer_is_forwarded_to_the_api(self) -> None:
        _, mock_post = _send(
            {"target": "chat-2", "message": "stop that", "steer": True},
            {"ok": True, "target": "chat-2", "started": False, "steered": True},
        )
        path, body = mock_post.call_args.args
        assert path == "/api/session-control/send"
        assert body == {"target": "chat-2", "message": "stop that", "steer": True}

    def test_omitting_steer_forwards_false_rather_than_nothing(self) -> None:
        """The API defaults it too, but an explicit false keeps the wire payload
        one shape: a missing key and a false key must not be two cases downstream."""
        _, mock_post = _send(
            {"target": "chat-2", "message": "later is fine"},
            {"ok": True, "target": "chat-2", "started": False, "steered": False},
        )
        assert mock_post.call_args.args[1]["steer"] is False


class TestOutcomeReports:
    def test_a_steered_delivery_says_it_cut_into_the_running_turn(self) -> None:
        out, _ = _send(
            {"target": "chat-2", "message": "stop that", "steer": True},
            {"ok": True, "target": "chat-2", "started": False, "steered": True},
        )
        assert "Steered" in out and "chat-2" in out
        assert "Queued" not in out

    def test_a_started_turn_is_reported_as_delivered(self) -> None:
        out, _ = _send(
            {"target": "chat-2", "message": "pick this up", "steer": True},
            {"ok": True, "target": "chat-2", "started": True, "steered": False},
        )
        assert "started a turn" in out

    def test_a_steer_that_fell_back_to_the_queue_says_so(self) -> None:
        """The caller asked for an interruption and did not get one. Reporting a
        plain queue would leave it believing the target was interrupted."""
        out, _ = _send(
            {"target": "chat-2", "message": "stop that", "steer": True},
            {"ok": True, "target": "chat-2", "started": False, "steered": False},
        )
        assert "Queued" in out
        assert "could not go into the running turn" in out

    def test_a_plain_queued_delivery_does_not_mention_steering(self) -> None:
        out, _ = _send(
            {"target": "chat-2", "message": "after this turn"},
            {"ok": True, "target": "chat-2", "started": False, "steered": False},
        )
        assert "Queued" in out
        assert "steer" not in out.lower()


class TestSchema:
    def test_a_non_boolean_steer_is_refused_at_the_schema(self) -> None:
        """The model supplies these arguments, so a string "true" is a real input
        shape. Coercing it would steer on a value the caller never meant as one."""
        with pytest.raises(ValidationError):
            validate_tool_args(
                {"target": "chat-2", "message": "hi", "steer": "true"}, SESSION_SEND_SCHEMA
            )

    def test_steer_defaults_to_false(self) -> None:
        cleaned = validate_tool_args({"target": "chat-2", "message": "hi"}, SESSION_SEND_SCHEMA)
        assert cleaned["steer"] is False
