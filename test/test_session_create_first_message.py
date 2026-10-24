"""``session_create`` may carry the new session's first message.

The message is delivered by ``send_to_target`` -- session_send's own path -- in
the same request that creates the session, so a caller never has to send to a key
it has not read back. Pinned here:

* a create with a message starts exactly one turn, attributed to the caller;
* a create without one starts nothing and reports no delivery, as before;
* a delivery failure still returns the key, plus the error, and keeps the session;
* the delivery is gated by the same ``authorize_target(operation="send")``;
* an oversize message is refused as session_send refuses it, before any create;
* the MCP tool forwards ``message`` and says what became of it.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import _peer_target

from kiro_crew import autonudge, autonudge_authz, mcp_dashboard_session_runs
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.validation_parts.field_engine import validate_tool_args
from kiro_crew.validation_parts.messaging_session_schemas import SESSION_CREATE_SCHEMA


@pytest.fixture(autouse=True)
def _env(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    _floor_monkeypatch.setattr(
        autonudge_authz, "sel", lambda: SimpleNamespace(log_tool_invocation=lambda **kw: None)
    )
    # No patrol store: the create's patrol arm is pinned elsewhere.
    _floor_monkeypatch.setattr(autonudge, "get_instance", lambda: None)
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


@pytest.fixture
def turns(monkeypatch) -> list[tuple[str, str]]:
    """Every turn a delivery started, as (slot key, prompt)."""
    seen: list[tuple[str, str]] = []

    async def _fake_run_chat(_state, slot, prompt):
        seen.append((slot.key, prompt))

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)
    return seen


def _request(state, caller, path: str, body: dict[str, Any]) -> MagicMock:
    request = MagicMock()
    request.app = {"state": state}
    request.path = path
    request.method = "POST"
    request.headers = {"X-Session-Key": slot_history_key(caller)}
    request.get = lambda key, default=None: (
        True if key in ("internal_auth", "peer_verified") else default
    )

    async def _json():
        return body

    request.json = _json
    return request


def _create(tmp_path, body: dict[str, Any]):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    request = _request(state, caller, "/api/session-control/create", body)

    async def _go():
        resp = await handlers_sc.api_session_control_create(request)
        target = json.loads(resp.body.decode()).get("target")
        slot = state.get_slot(target) if target else None
        task = getattr(slot, "task", None)
        # Wait on the started turn itself; a create that delivered nothing has none.
        if task is not None:
            await asyncio.wait_for(task, timeout=30)
        return resp, task

    resp, task = asyncio.run(_go())
    return resp.status, json.loads(resp.body.decode()), state, task


def test_a_message_starts_exactly_one_turn_attributed_to_the_caller(tmp_path, turns):
    status, body, state, task = _create(tmp_path, {"title": "worker", "message": "do the thing"})

    assert status == 200, body
    target = body["target"]
    assert state.get_slot(target) is not None
    assert task is not None and task.done()
    assert body["delivery"] == {"ok": True, "target": target, "started": True, "steered": False}
    assert "delivery_error" not in body
    assert len(turns) == 1
    slot_key, prompt = turns[0]
    assert slot_key == target
    assert prompt.startswith("[sent by session chat-1 via session_create]")
    assert prompt.rstrip().endswith("do the thing")


def test_without_a_message_nothing_runs_and_no_delivery_is_reported(tmp_path, turns):
    status, body, state, task = _create(tmp_path, {"title": "worker"})

    assert status == 200, body
    assert state.get_slot(body["target"]) is not None
    assert "delivery" not in body
    assert "delivery_error" not in body
    assert task is None
    assert turns == []


def test_a_delivery_failure_still_returns_the_key_and_keeps_the_session(
    tmp_path, turns, monkeypatch
):
    async def _refuse(*_a, **_kw):
        raise sc.SessionControlError("target busy", status=409, code="target_busy")

    monkeypatch.setattr(sc, "send_to_target", _refuse)
    status, body, state, task = _create(tmp_path, {"title": "worker", "message": "hello"})

    assert status == 200, body
    assert body["target"]
    assert state.get_slot(body["target"]) is not None
    assert body["delivery_error"] == {"error": "target busy", "code": "target_busy"}
    assert "delivery" not in body
    assert task is None
    assert turns == []


def test_an_unexpected_delivery_crash_still_returns_the_key(tmp_path, turns, monkeypatch):
    async def _crash(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(sc, "send_to_target", _crash)
    status, body, state, task = _create(tmp_path, {"title": "worker", "message": "hello"})

    assert status == 200, body
    assert state.get_slot(body["target"]) is not None
    assert body["delivery_error"]["code"] == "delivery_failed"


def test_the_delivery_goes_through_the_send_authorization_gate(tmp_path, turns, monkeypatch):
    """A caller the send gate refuses cannot seed the session through create."""
    ops: list[str] = []

    def _deny(state, *, caller_session_key, target, operation, **_kw):
        ops.append(operation)
        raise sc.SessionControlError("not yours", status=403, code="not_authorized")

    monkeypatch.setattr(sc, "authorize_target", _deny)
    status, body, _state, task = _create(tmp_path, {"title": "worker", "message": "hello"})

    assert status == 200, body
    assert ops == ["send"]
    assert body["delivery_error"]["code"] == "not_authorized"
    assert task is None
    assert turns == []


def test_an_oversize_message_is_refused_as_session_send_refuses_it(tmp_path, turns):
    big = "x" * (sc.MAX_SEND_MESSAGE_CHARS + 1)
    status, body, state, task = _create(tmp_path, {"title": "worker", "message": big})

    assert status == 400
    # Nothing was created: only the caller's own slot exists.
    assert list(state._slots) == ["chat-1"]

    # session_send's own refusal of the same text, for the same caller.
    caller = state.get_slot("chat-1")
    _peer_target(state, "chat-2", caller)
    send = _request(
        state, caller, "/api/session-control/send", {"target": "chat-2", "message": big}
    )
    sent = asyncio.run(handlers_sc.api_session_control_send(send))
    assert sent.status == 400
    assert json.loads(sent.body.decode()) == body
    assert body["code"] == "message_too_long"
    assert task is None
    assert turns == []


def test_a_blank_message_is_refused_before_any_create(tmp_path, turns):
    status, body, state, task = _create(tmp_path, {"title": "worker", "message": "   "})

    assert status == 400
    assert body["code"] == "message_required"
    assert list(state._slots) == ["chat-1"]


def test_the_tool_schema_bounds_message_like_session_send():
    from kiro_crew.validation_parts.messaging_session_schemas import SESSION_SEND_SCHEMA

    create_max = next(f for f in SESSION_CREATE_SCHEMA.fields if f.name == "message").max_len
    send_max = next(f for f in SESSION_SEND_SCHEMA.fields if f.name == "message").max_len
    assert create_max == send_max
    assert validate_tool_args({"message": "hi"}, SESSION_CREATE_SCHEMA)["message"] == "hi"


class _Client:
    def __init__(self, resp: dict[str, Any]) -> None:
        self.resp = resp
        self.posts: list[dict[str, Any]] = []

    def post(self, path, payload, **_kw):
        self.posts.append(payload)
        return self.resp if not payload.get("dry_run") else {"ok": True}


@pytest.mark.parametrize(
    ("resp", "expected"),
    [
        ({"delivery": {"started": True}}, "It started a turn on your first message"),
        ({"delivery": {"started": False}}, "Your first message is queued"),
        (
            {"delivery_error": {"error": "target busy", "code": "target_busy"}},
            "Your first message was NOT delivered: target busy (target_busy)",
        ),
    ],
)
def test_the_tool_forwards_message_and_reports_its_fate(resp, expected):
    client = _Client({"target": "chat-7", "title": "w", **resp})
    ctx = SimpleNamespace(client=client, caller_key="chat-1")

    out = mcp_dashboard_session_runs._run_session_create(
        {"title": "w", "agent": "kirocrew-worker", "message": "go"}, ctx
    )

    assert client.posts[-1]["message"] == "go"
    assert expected in out
    assert "It is empty" not in out


def test_the_tool_without_message_still_says_the_session_is_empty():
    client = _Client({"target": "chat-7", "title": "w"})
    ctx = SimpleNamespace(client=client, caller_key="chat-1")

    out = mcp_dashboard_session_runs._run_session_create({"title": "w"}, ctx)

    assert "message" not in client.posts[-1]
    assert "It is empty and waiting" in out
