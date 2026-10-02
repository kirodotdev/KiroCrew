"""``session_continue``: hand a peer session's thread back to its agent, as Continue does.

The verb is ``chat_handlers.continue_slot_turn`` without ``require_interrupted``.
The tests pin the case ``session_retry`` cannot reach (a turn that looks finished
because a force-quit left no error row), the body the result reports, the
refusals a caller can hit, the re-check under the slot lock, the HTTP route and
the MCP tool's rendering.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_handlers as ch
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import (
    _MANUAL_CONTINUE_MSG,
    _MANUAL_RESUME_MSG,
    SESSION_START_FAILED_KIND,
    SYNTHETIC_RECOVERY_KIND,
    slot_history_key,
)
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import _call_tool_inner

_START_TIMEOUT = "Request initialize timed out after 90s"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture
def dispatched(monkeypatch):
    """Stub the real turn dispatcher and record the target's audit lines."""
    started = AsyncMock(return_value=True)
    mock_sel = MagicMock()
    monkeypatch.setattr(ch, "_start_next_queued_turn", started)
    monkeypatch.setattr(ch, "sel", lambda: mock_sel)
    return SimpleNamespace(started=started, sel=mock_sel)


@pytest.fixture
def audits(monkeypatch):
    lines: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: lines.append(kw))
    return lines


def _key(slot) -> str:
    return slot_history_key(slot)


def _finished(slot) -> None:
    """What a force-quit turn looks like: the reply rows, and no error row after them."""
    slot.append("user", "refactor the parser", "msg msg-u")
    slot.append("assistant", "Started on the parser; editing lexer.py", "msg msg-a")


def _continue(state, caller, target: str = "chat-2", **kw) -> dict:
    return asyncio.run(
        sc.continue_target(state, caller_session_key=_key(caller), target=target, **kw)
    )


def _refused(state, caller, target: str = "chat-2", **kw) -> sc.SessionControlError:
    with pytest.raises(sc.SessionControlError) as exc:
        _continue(state, caller, target, **kw)
    return exc.value


def test_a_turn_that_looks_finished_is_continued(tmp_path, dispatched, audits):
    """The case session_retry refuses with turn_not_failed.

    Mutation guard: passing ``require_interrupted=True`` here turns this verb
    back into session_retry and this test red.
    """
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _finished(target)
    rows_before = list(target.messages)

    out = _continue(state, caller)

    assert out == {"ok": True, "target": "chat-2", "body": "continued"}
    assert target.messages == rows_before, "the call itself writes no transcript row"
    (entry,) = list(target._queue)
    assert entry["kind"] == SYNTHETIC_RECOVERY_KIND
    assert entry["content"] == _MANUAL_CONTINUE_MSG
    assert entry["meta"][sc.SEND_ORIGIN_META_KEY]["slot"] == "chat-1"
    dispatched.started.assert_awaited_once()
    tool_line = dispatched.sel.log_tool_invocation.call_args.kwargs
    assert tool_line["tool_name"] == "dashboard_continue"
    assert tool_line["metadata"] == {"slot": "chat-2", "via": "session_control"}
    assert audits[-1]["operation"] == "continue"
    assert audits[-1]["outcome"] == "allowed"
    assert audits[-1]["caller_session_key"] == _key(caller)
    assert audits[-1]["detail"] == {"body": "continued"}


def test_an_interrupted_turn_gets_the_pick_up_body(tmp_path, dispatched, audits):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.append("user", "do the thing", "msg msg-u")
    target.append("error", _START_TIMEOUT, "msg msg-err", meta={"kind": SESSION_START_FAILED_KIND})

    out = _continue(state, caller)

    assert out["body"] == "resumed"
    (entry,) = list(target._queue)
    assert entry["content"] == _MANUAL_RESUME_MSG
    assert audits[-1]["detail"] == {"body": "resumed"}


def test_the_agent_request_does_not_carry_the_human_flag(tmp_path, dispatched, audits):
    """Mutation guard: ``directive_user_origin=True`` would let an agent's call
    gain the authenticated-human flag a Continue press from the owner carries."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _finished(target)

    _continue(state, caller)

    (entry,) = list(target._queue)
    assert not entry.get("_directive_user_origin")


def test_a_running_target_is_refused(tmp_path, dispatched, audits):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _finished(target)
    task = MagicMock()
    task.done.return_value = False
    target.task = task
    assert target.running

    err = _refused(state, caller)

    assert err.code == "slot_running"
    assert target.queue_depth == 0
    dispatched.started.assert_not_awaited()
    assert audits[-1]["outcome"] == "denied"
    assert audits[-1]["operation"] == "continue"


def test_queued_messages_are_refused(tmp_path, dispatched, audits):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _finished(target)
    target.queue_insert(0, "next thing")

    assert _refused(state, caller).code == "slot_queue_pending"
    dispatched.started.assert_not_awaited()


def test_an_empty_session_is_refused(tmp_path, dispatched, audits):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-2")

    assert _refused(state, caller).code == "slot_empty"
    dispatched.started.assert_not_awaited()


def test_a_target_the_caller_did_not_create_is_refused(tmp_path, dispatched, audits):
    """A fenced caller reaches only its own children, as it does for stop and send."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target._created_by = "chat-someone-else"
    _finished(target)

    err = _refused(state, caller, caller_fenced=True)

    assert err.code == "not_creator"
    assert target.queue_depth == 0
    dispatched.started.assert_not_awaited()


def test_a_fenced_caller_can_continue_what_it_created(tmp_path, dispatched, audits):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target._created_by = caller.key
    _finished(target)

    assert _continue(state, caller, caller_fenced=True)["ok"] is True


def test_a_session_cannot_continue_itself(tmp_path, dispatched, audits):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    _finished(caller)

    assert _refused(state, caller, target="chat-1").code == "self_target"
    dispatched.started.assert_not_awaited()


def test_continues_repeat_refusal_passes_through(tmp_path, dispatched, audits):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.append("user", "do the thing", "msg msg-u")
    target.append("error", _START_TIMEOUT, "msg msg-err", meta={"kind": SESSION_START_FAILED_KIND})
    target.append("inject", "[Continue]", "msg msg-inject", meta={"injectKind": "recovery"})
    target.append("error", _START_TIMEOUT, "msg msg-err2", meta={"kind": SESSION_START_FAILED_KIND})

    err = _refused(state, caller)

    assert err.code == "session_start_repeat"
    dispatched.started.assert_not_awaited()


def test_a_remote_bound_target_is_refused(tmp_path, dispatched, audits):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _finished(target)
    target.executor = "remote"

    assert _refused(state, caller).code == "remote_target_unsupported"
    dispatched.started.assert_not_awaited()


def test_a_target_linked_while_the_lock_was_awaited_is_refused(
    tmp_path, dispatched, audits, monkeypatch
):
    """Mutation guard: without ``before_dispatch`` the continuation lands on a
    session that became channel-backed after the first check passed."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _finished(target)

    async def _link_meanwhile(*_a, **_kw):
        target.linked_session_key = "slack:1786300000.000100"
        return None

    monkeypatch.setattr(ch, "_subagents_attached_response", _link_meanwhile)

    assert _refused(state, caller).code == "linked_session_target"
    assert target.queue_depth == 0
    dispatched.started.assert_not_awaited()


# ── HTTP handler ─────────────────────────────────────────────────────────────


def _handler_request(monkeypatch):
    req = MagicMock()
    req.app = {"state": MagicMock()}
    monkeypatch.setattr(handlers_sc, "_require_internal", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers_sc, "_body", AsyncMock(return_value={"target": "chat-2"}))
    monkeypatch.setattr(handlers_sc, "_read_session_key", lambda _r: "dashboard:chat-1")
    monkeypatch.setattr(handlers_sc, "_carried_fence", lambda _r: None)
    return req


def test_the_handler_returns_the_body(monkeypatch):
    req = _handler_request(monkeypatch)
    seen: dict = {}

    async def _ok(_state, **kw):
        seen.update(kw)
        return {"ok": True, "target": "chat-2", "body": "resumed"}

    monkeypatch.setattr(sc, "continue_target", _ok)
    resp = asyncio.run(handlers_sc.api_session_control_continue(req))
    assert resp.status == 200
    assert json.loads(resp.body)["body"] == "resumed"
    assert seen["caller_session_key"] == "dashboard:chat-1"
    assert seen["target"] == "chat-2"


def test_the_handler_renders_a_refusal_with_its_code(monkeypatch):
    req = _handler_request(monkeypatch)

    async def _running(*_a, **_kw):
        raise sc.SessionControlError("slot is running", status=409, code="slot_running")

    monkeypatch.setattr(sc, "continue_target", _running)
    resp = asyncio.run(handlers_sc.api_session_control_continue(req))
    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "slot_running"


def test_the_route_is_registered_and_strict_internal():
    from aiohttp import web

    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS, _register_mcp_routes

    assert "/api/session-control/continue" in _STRICT_INTERNAL_API_PATHS
    app = web.Application()
    _register_mcp_routes(app)
    paths = {getattr(r.resource, "canonical", "") for r in app.router.routes()}
    assert "/api/session-control/continue" in paths


# ── MCP tool ─────────────────────────────────────────────────────────────────

_VERIFIED = "dashboard:chat-verified"


@pytest.mark.parametrize(
    ("body", "phrase"),
    [("resumed", "was interrupted"), ("continued", "had ended")],
)
def test_tool_carries_the_verified_key_and_reports_the_body(body, phrase):
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch(
            "kiro_crew.mcp_dashboard._post",
            return_value={"ok": True, "target": "chat-2", "body": body},
        ) as post,
    ):
        out = _call_tool_inner("session_continue", {"target": "chat-2"})
    assert post.call_args.args[0] == "/api/session-control/continue"
    assert post.call_args.args[1] == {"target": "chat-2"}
    assert post.call_args.kwargs["session_key"] == _VERIFIED
    assert f"Continue sent to `chat-2` ({body})" in out
    assert phrase in out


def test_tool_reports_a_refusal_as_an_error():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post", return_value={"error": "slot is running"}),
    ):
        out = _call_tool_inner("session_continue", {"target": "chat-2"})
    assert out.startswith("Error:")
    assert "slot is running" in out


def test_tool_takes_no_prompt_text():
    """Keep-off: the tool never edits or replaces the prompt."""
    from kiro_crew.mcp_dashboard import _tool_definitions

    (tool,) = [t for t in _tool_definitions() if t["name"] == "session_continue"]
    assert set(tool["inputSchema"]["properties"]) == {"target"}
