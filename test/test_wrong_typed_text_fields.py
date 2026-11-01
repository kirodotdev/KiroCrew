"""A text field holding a number, list, object, bool or null is a 400, not a 500.

A JSON value that is not a string has no ``.strip()``, so each route below checks
the field's type before it strips it, and answers ``string_field_error``'s 400
(``code: invalid_field_type``) for anything else.

Every route also runs a string value and a missing field: those take the same
path a string always takes, and only a non-string is refused.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state, _make_tags_app
from handlers_messaging_coverage_helpers import _payload, _Req, _run, _state

import kiro_crew.dashboard.handlers.messaging as messaging

#: JSON values that are not strings: none of them has a ``.strip()``.
_NOT_STRINGS = [5, 1.5, True, [], ["a"], {}, {"a": "b"}, None]


def _refusal(field: str) -> dict[str, str]:
    return {"error": f"{field} must be a string", "code": "invalid_field_type"}


def test_the_helper_passes_a_string_and_refuses_the_rest() -> None:
    from kiro_crew.dashboard.handlers._shared import string_field_error

    assert string_field_error("f", "") is None
    assert string_field_error("f", "  text ") is None
    for value in _NOT_STRINGS:
        refusal = string_field_error("f", value)
        assert refusal is not None and refusal.status == 400
        assert _payload(refusal) == _refusal("f")


# ── POST /api/delete-message ──────────────────────────────────────────────────


@pytest.mark.parametrize("field", ["channel", "ts"])
@pytest.mark.parametrize("value", _NOT_STRINGS, ids=repr)
def test_delete_message_refuses_a_non_string_field(field: str, value: Any) -> None:
    body = {"channel": "C0123456789", "ts": "1700000000.000100", field: value}
    resp = _run(messaging.api_delete_message, _Req(_state(), body))
    assert resp.status == 400
    assert _payload(resp) == _refusal(field)


@pytest.mark.parametrize(
    "body",
    [{}, {"channel": "", "ts": ""}, {"channel": "  ", "ts": "1"}, {"channel": "C1"}],
    ids=repr,
)
def test_delete_message_keeps_its_answer_for_blank_or_missing_strings(body: dict) -> None:
    resp = _run(messaging.api_delete_message, _Req(_state(), body))
    assert resp.status == 400
    assert _payload(resp) == {"error": "channel and ts required"}


def test_delete_message_still_deletes_with_padded_strings() -> None:
    slack = MagicMock()

    async def _delete(channel: str, ts: str) -> None:
        slack.called_with = (channel, ts)

    slack.delete_message = _delete
    body = {"channel": " C0123456789 ", "ts": " 1700000000.000100 "}
    resp = _run(messaging.api_delete_message, _Req(_state(slack_client=slack), body))
    assert resp.status == 200
    assert slack.called_with == ("C0123456789", "1700000000.000100")


# ── POST /api/send-message ────────────────────────────────────────────────────


@pytest.mark.parametrize("value", _NOT_STRINGS, ids=repr)
def test_send_message_refuses_a_non_string_text(value: Any) -> None:
    resp = _run(messaging.api_send_message, _Req(_state(), {"text": value}))
    assert resp.status == 400
    assert _payload(resp) == _refusal("text")


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "   "}], ids=repr)
def test_send_message_keeps_its_answer_for_blank_or_missing_text(body: dict) -> None:
    resp = _run(messaging.api_send_message, _Req(_state(), body))
    assert resp.status == 400
    assert _payload(resp) == {"error": "text required"}


@pytest.mark.parametrize("field", ["channel", "user"])
@pytest.mark.parametrize("value", _NOT_STRINGS, ids=repr)
def test_send_message_refuses_a_non_string_slack_target(field: str, value: Any) -> None:
    resp = _run(messaging.api_send_message, _Req(_state(), {"text": "hi", field: value}))
    assert resp.status == 400
    assert _payload(resp) == _refusal(field)


# ── POST /api/chat ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("value", _NOT_STRINGS, ids=repr)
async def test_chat_send_refuses_a_non_string_message(tmp_path, monkeypatch, value: Any) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat", json={"slot": "s1", "message": value})
        assert resp.status == 400
        assert await resp.json() == _refusal("message")
    assert not state.has_slot("s1")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"slot": "s1"}, {"slot": "s1", "message": "  "}], ids=repr)
async def test_chat_send_keeps_its_answer_for_blank_or_missing_message(
    tmp_path, monkeypatch, body: dict
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat", json=body)
        assert resp.status == 400
        assert await resp.json() == {"error": "message is required", "code": "message_required"}


# ── POST /api/chat/tags ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [5, 1.5, True, ["a"], {"a": "b"}], ids=repr)
async def test_tag_create_refuses_a_non_string_name(tmp_path, monkeypatch, value: Any) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.post("/api/chat/tags", json={"name": value})
        assert resp.status == 400
        assert await resp.json() == _refusal("name")
        listed = await (await client.get("/api/chat/tags")).json()
    assert listed == [] or all(t.get("name") != str(value) for t in listed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    # A falsy value reads as "", so it gets the same "name required".
    [{}, {"name": ""}, {"name": "   "}, {"name": None}, {"name": 0}, {"name": False}, {"name": []}],
    ids=repr,
)
async def test_tag_create_keeps_name_required_for_blank_or_missing_name(
    tmp_path, monkeypatch, body: dict
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.post("/api/chat/tags", json=body)
        assert resp.status == 400
        assert await resp.json() == {"error": "name required", "code": "name_required"}


@pytest.mark.asyncio
async def test_tag_create_still_creates_from_a_padded_string(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.post("/api/chat/tags", json={"name": "  Review  "})
        assert resp.status in (200, 201)
        assert (await resp.json())["name"] == "Review"
