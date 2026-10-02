"""``session_retry``: re-run a peer session's failed last turn, as Resume does.

The verb reuses ``chat_handlers.continue_slot_turn``, the mechanism behind the
Continue endpoint, with ``require_interrupted=True``. The tests cover the
refusals a caller can hit (ownership, a running target, a turn that did not
fail, Resume's own repeat refusal, a target that moved out of reach while the
slot lock was awaited), the one success shape, and the MCP tool's rendering.
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


def _failed(slot, text: str = "do the thing") -> None:
    slot.append("user", text, "msg msg-u")
    slot.append("error", _START_TIMEOUT, "msg msg-err", meta={"kind": SESSION_START_FAILED_KIND})


def _retry(state, caller, target: str = "chat-2", **kw) -> dict:
    return asyncio.run(sc.retry_target(state, caller_session_key=_key(caller), target=target, **kw))


def _refused(state, caller, target: str = "chat-2", **kw) -> sc.SessionControlError:
    with pytest.raises(sc.SessionControlError) as exc:
        _retry(state, caller, target, **kw)
    return exc.value


def _user_rows(slot) -> int:
    return sum(1 for m in slot.messages if m.get("role") == "user")


def test_a_failed_turn_is_resumed_without_a_second_user_message(tmp_path, dispatched, audits):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _failed(target)
    rows_before = list(target.messages)

    out = _retry(state, caller)

    assert out == {"ok": True, "target": "chat-2"}
    assert target.messages == rows_before, "the retry itself writes no transcript row"
    assert _user_rows(target) == 1
    (entry,) = list(target._queue)
    assert entry["kind"] == SYNTHETIC_RECOVERY_KIND
    assert entry["content"] == _MANUAL_RESUME_MSG
    assert entry["meta"][sc.SEND_ORIGIN_META_KEY]["slot"] == "chat-1"
    dispatched.started.assert_awaited_once()
    tool_line = dispatched.sel.log_tool_invocation.call_args.kwargs
    assert tool_line["tool_name"] == "dashboard_continue"
    assert tool_line["metadata"] == {"slot": "chat-2", "via": "session_control"}
    assert audits[-1]["operation"] == "retry"
    assert audits[-1]["outcome"] == "allowed"
    assert audits[-1]["caller_session_key"] == _key(caller)


def test_retry_dispatches_when_continue_audit_initialization_fails(
    tmp_path, dispatched, audits, monkeypatch
):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _failed(target)

    def _raise_sel():
        raise RuntimeError("SEL initialization failed")

    monkeypatch.setattr(ch, "sel", _raise_sel)

    assert _retry(state, caller) == {"ok": True, "target": "chat-2"}
    dispatched.started.assert_awaited_once()


def test_a_turn_that_ended_with_no_reply_is_retried(tmp_path, dispatched, audits):
    """A gateway restart mid-turn leaves only the user row; that is a failure too."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.append("user", "do the thing", "msg msg-u")

    assert _retry(state, caller)["ok"] is True
    dispatched.started.assert_awaited_once()


def test_a_turn_that_succeeded_is_refused(tmp_path, dispatched, audits):
    """Mutation guard: dropping ``require_interrupted`` makes this a regenerate."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.append("user", "hi", "msg msg-u")
    target.append("assistant", "all done", "msg msg-a")
    before = list(target.messages)

    err = _refused(state, caller)

    assert err.code == "turn_not_failed"
    assert err.status == 409
    assert target.messages == before
    assert target.queue_depth == 0
    dispatched.started.assert_not_awaited()
    assert audits[-1]["outcome"] == "denied"
    assert audits[-1]["detail"] == {"code": "turn_not_failed"}


def test_a_turn_the_user_stopped_is_refused(tmp_path, dispatched, audits):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.append("user", "hi", "msg msg-u")
    target.append("inject", "Stopped", "msg msg-inject", meta={"kind": "stop_event"})

    assert _refused(state, caller).code == "turn_not_failed"
    dispatched.started.assert_not_awaited()


def test_a_running_target_is_refused(tmp_path, dispatched, audits):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _failed(target)
    task = MagicMock()
    task.done.return_value = False
    target.task = task
    assert target.running

    assert _refused(state, caller).code == "slot_running"
    assert target.queue_depth == 0
    dispatched.started.assert_not_awaited()


def test_a_target_the_caller_did_not_create_is_refused(tmp_path, dispatched, audits):
    """A fenced caller (a crew member admitted by the HTTP gate) reaches only its
    own children, exactly as it does for stop and send."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target._created_by = "chat-someone-else"
    _failed(target)

    err = _refused(state, caller, caller_fenced=True)

    assert err.code == "not_creator"
    assert target.queue_depth == 0
    dispatched.started.assert_not_awaited()


def test_a_fenced_caller_can_retry_what_it_created(tmp_path, dispatched, audits):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target._created_by = caller.key
    _failed(target)

    assert _retry(state, caller, caller_fenced=True)["ok"] is True


def test_resumes_repeat_refusal_passes_through(tmp_path, dispatched, audits):
    """Two failed starts with only a Resume between them: a third start is the
    same start, and the verb returns Continue's own refusal unchanged."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _failed(target)
    target.append("inject", "[Continue]", "msg msg-inject", meta={"injectKind": "recovery"})
    target.append("error", _START_TIMEOUT, "msg msg-err", meta={"kind": SESSION_START_FAILED_KIND})

    err = _refused(state, caller)

    assert err.code == "session_start_repeat"
    assert "restart" in err.message
    dispatched.started.assert_not_awaited()


def test_a_remote_bound_target_is_refused(tmp_path, dispatched, audits):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _failed(target)
    target.executor = "remote"

    assert _refused(state, caller).code == "remote_target_unsupported"
    dispatched.started.assert_not_awaited()


def test_a_target_linked_while_the_lock_was_awaited_is_refused(
    tmp_path, dispatched, audits, monkeypatch
):
    """The gate re-runs under the slot lock, after the verb's awaits.

    Mutation guard: without ``before_dispatch`` the continuation lands on a
    session that became channel-backed after the first check passed.
    """
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _failed(target)

    async def _link_meanwhile(*_a, **_kw):
        target.linked_session_key = "slack:1786300000.000100"
        return None

    monkeypatch.setattr(ch, "_subagents_attached_response", _link_meanwhile)

    assert _refused(state, caller).code == "linked_session_target"
    assert target.queue_depth == 0
    dispatched.started.assert_not_awaited()


# ── HTTP handler ─────────────────────────────────────────────────────────────


def test_the_handler_renders_a_refusal_with_its_code(monkeypatch):
    req = MagicMock()
    req.app = {"state": MagicMock()}
    monkeypatch.setattr(handlers_sc, "_require_internal", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers_sc, "_body", AsyncMock(return_value={"target": "chat-2"}))
    monkeypatch.setattr(handlers_sc, "_read_session_key", lambda _r: "dashboard:chat-1")
    monkeypatch.setattr(handlers_sc, "_carried_fence", lambda _r: None)

    async def _not_failed(*_a, **_kw):
        raise sc.SessionControlError("nothing to retry", status=409, code="turn_not_failed")

    monkeypatch.setattr(sc, "retry_target", _not_failed)
    resp = asyncio.run(handlers_sc.api_session_control_retry(req))
    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "turn_not_failed"


def test_the_route_is_in_the_strict_internal_set():
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/session-control/retry" in _STRICT_INTERNAL_API_PATHS


# ── MCP tool ─────────────────────────────────────────────────────────────────

_VERIFIED = "dashboard:chat-verified"


def test_tool_carries_the_verified_key_and_reports_the_retry():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch(
            "kiro_crew.mcp_dashboard._post", return_value={"ok": True, "target": "chat-2"}
        ) as post,
    ):
        out = _call_tool_inner("session_retry", {"target": "chat-2"})
    assert post.call_args.args[0] == "/api/session-control/retry"
    assert post.call_args.args[1] == {"target": "chat-2"}
    assert post.call_args.kwargs["session_key"] == _VERIFIED
    assert "Retry started in `chat-2`" in out


def test_tool_reports_a_refusal_as_an_error():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch(
            "kiro_crew.mcp_dashboard._post",
            return_value={"error": "the last turn did not fail; there is nothing to retry"},
        ),
    ):
        out = _call_tool_inner("session_retry", {"target": "chat-2"})
    assert out.startswith("Error:")
    assert "nothing to retry" in out
