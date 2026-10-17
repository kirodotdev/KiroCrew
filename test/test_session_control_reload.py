"""``session_reload``: relaunch a created session's agent process from another session.

The verb shares ``chat_handlers.reload_slot_session`` with the tab menu's Reload
session route. These tests cover the verb's own gate (created-by, self, remote,
busy, queued, sub-agents, the usual containment), that the transcript is left as
it was plus one notice naming the caller, the SEL trail, the HTTP route and the
MCP tool's rendering.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture
def eager(monkeypatch):
    spawn = MagicMock(return_value=None)
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", spawn)
    return spawn


@pytest.fixture
def audits(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: calls.append(kw))
    return calls


def _key(slot) -> str:
    return slot_history_key(slot)


def _idle_provider():
    provider = MagicMock()
    provider.has_active_turn = MagicMock(return_value=False)
    return provider


def _world(tmp_path, *, created: bool = True):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    if created:
        target._created_by = caller.key
    state.sessions.get_provider = MagicMock(return_value=_idle_provider())
    state.sessions.reset = AsyncMock(return_value=True)
    return state, caller, target


def _reload(state, caller, target: str = "chat-2") -> dict:
    return asyncio.run(sc.reload_target(state, caller_session_key=_key(caller), target=target))


def _refused(state, caller, target: str = "chat-2") -> sc.SessionControlError:
    with pytest.raises(sc.SessionControlError) as exc:
        _reload(state, caller, target)
    return exc.value


def _nothing_happened(state, target, before: list, eager) -> None:
    state.sessions.reset.assert_not_awaited()
    assert target.messages == before
    eager.assert_not_called()


# ── Happy path ───────────────────────────────────────────────────────────────


def test_a_created_idle_session_is_reset_and_respawned(tmp_path, eager, audits):
    state, caller, target = _world(tmp_path)

    out = _reload(state, caller)

    assert out == {"ok": True, "target": "chat-2"}
    state.sessions.reset.assert_awaited_once_with("dashboard:chat-2", skip_if_busy=True)
    eager.assert_called_once()
    assert eager.call_args.kwargs.get("allow_resume") is True
    assert audits[-1]["operation"] == "reload"
    assert audits[-1]["outcome"] == "allowed"
    assert audits[-1]["caller_session_key"] == _key(caller)
    assert audits[-1]["slot_key"] == "chat-2"


def test_the_transcript_is_untouched_except_one_notice_naming_the_caller(tmp_path, eager):
    """Mutation guard: a reload that rewrote or dropped history, or appended an
    unattributed notice, fails here."""
    state, caller, target = _world(tmp_path)
    target.append("user", "hello", "msg msg-u")
    target.append("assistant", "hi there", "msg msg-a")
    before = [dict(m) for m in target.messages]

    _reload(state, caller)

    assert target.messages[: len(before)] == before
    assert len(target.messages) == len(before) + 1
    notice = target.messages[-1]
    assert notice["role"] == "assistant"
    assert notice["meta"]["kind"] == "session_reload"
    assert notice["meta"]["kind"] == "session_reload"
    assert "reloaded_by" not in notice["meta"]
    assert "Reloading session" in notice["content"]
    assert "`chat-1`" in notice["content"]


def test_the_agent_model_and_workspace_are_kept(tmp_path, eager):
    state, caller, target = _world(tmp_path)
    target.model = "some-model"
    before = (target.agent, target.model, target.workspace)

    _reload(state, caller)

    assert (target.agent, target.model, target.workspace) == before


# ── Scope ────────────────────────────────────────────────────────────────────


def test_a_session_the_caller_did_not_create_is_refused(tmp_path, eager, audits):
    """The owner's own tab is reachable by session_stop, but not by reload."""
    state, caller, target = _world(tmp_path, created=False)
    before = list(target.messages)

    err = _refused(state, caller)

    assert err.code == "not_creator"
    _nothing_happened(state, target, before, eager)
    assert audits[-1]["outcome"] == "denied"
    assert audits[-1]["detail"] == {"code": "not_creator"}


def test_a_session_created_by_someone_else_is_refused(tmp_path, eager):
    state, caller, target = _world(tmp_path, created=False)
    target._created_by = "chat-9"

    assert _refused(state, caller).code == "not_creator"
    state.sessions.reset.assert_not_awaited()


def test_self_reload_is_refused(tmp_path, eager):
    state, caller, _target = _world(tmp_path)
    before = list(caller.messages)

    err = _refused(state, caller, target="chat-1")

    assert err.code == "self_target"
    _nothing_happened(state, caller, before, eager)


def test_a_channel_linked_target_is_refused(tmp_path, eager):
    state, caller, target = _world(tmp_path)
    target.linked_session_key = "slack:1786300000.000100"

    assert _refused(state, caller).code == "linked_session_target"
    state.sessions.reset.assert_not_awaited()


def test_an_incognito_target_is_refused(tmp_path, eager):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    hidden = state.get_or_create_slot("chat-hidden", memory_mode="incognito")
    hidden._created_by = caller.key
    state.sessions.reset = AsyncMock(return_value=True)

    assert _refused(state, caller, target="chat-hidden").code == "ephemeral_target"
    state.sessions.reset.assert_not_awaited()


def test_a_remote_crew_target_is_refused(tmp_path, eager):
    state, caller, target = _world(tmp_path)
    target.executor = "remote"

    assert _refused(state, caller).code == "relay_archive_read_only"
    state.sessions.reset.assert_not_awaited()


# ── Busy ─────────────────────────────────────────────────────────────────────


def _running(slot):
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def test_a_running_turn_is_refused(tmp_path, eager):
    state, caller, target = _world(tmp_path)
    _running(target)
    before = list(target.messages)

    err = _refused(state, caller)

    assert err.code == "target_busy"
    _nothing_happened(state, target, before, eager)


def test_a_turn_the_provider_reports_is_refused(tmp_path, eager):
    from kiro_crew.providers.base import LLMProvider

    state, caller, target = _world(tmp_path)
    busy = MagicMock(spec=LLMProvider)
    busy.has_active_turn = MagicMock(return_value=True)
    state.sessions.get_provider = MagicMock(return_value=busy)

    assert _refused(state, caller).code == "target_busy"
    state.sessions.reset.assert_not_awaited()


def test_queued_messages_are_refused(tmp_path, eager):
    """A queue about to drain is work the caller would pull the process out from under."""
    state, caller, target = _world(tmp_path)
    target._queue.append({"id": "q1", "content": "next"})

    assert _refused(state, caller).code == "target_busy"
    state.sessions.reset.assert_not_awaited()
    assert target._queue == [{"id": "q1", "content": "next"}]


def test_attached_subagents_are_refused(tmp_path, monkeypatch, eager):
    state, caller, target = _world(tmp_path)

    async def _children(*_a, **_kw):
        return chat_handlers.web.json_response(
            {"error": "sub-agents are running", "code": "slot_subagents_running"}, status=409
        )

    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", _children)
    before = list(target.messages)

    assert _refused(state, caller).code == "target_busy"
    _nothing_happened(state, target, before, eager)


def test_a_turn_starting_inside_the_locks_is_refused(tmp_path, monkeypatch, eager):
    """The busy probe runs again inside the teardown's locks. Mutation guard:
    passing the route's provider-only probe would miss a cold-starting turn."""
    state, caller, target = _world(tmp_path)
    # The first probe is the verb's own, before any lock; a turn dispatched
    # after it is visible only to the probe the teardown runs under its locks.
    original_lock = chat_handlers._slot_switch_session_lock

    def _lock_then_start(session_key):
        _running(target)
        return original_lock(session_key)

    monkeypatch.setattr(chat_handlers, "_slot_switch_session_lock", _lock_then_start)

    assert _refused(state, caller).code == "target_busy"
    state.sessions.reset.assert_not_awaited()


def test_a_declined_reset_under_a_racing_turn_is_busy(tmp_path, eager):
    """The shared teardown's post-reset re-check reports its own 409 as target_busy."""
    state, caller, target = _world(tmp_path)
    idle = _idle_provider()
    busy = MagicMock()
    busy.has_active_turn = MagicMock(return_value=True)
    probes = iter([idle, idle, idle])
    state.sessions.get_provider = MagicMock(side_effect=lambda _k: next(probes, busy))
    state.sessions.reset = AsyncMock(return_value=False)
    before = list(target.messages)

    assert _refused(state, caller).code == "target_busy"
    assert target.messages == before
    eager.assert_not_called()


# ── Re-authorization inside the locks ────────────────────────────────────────


async def _queued_on(lock: asyncio.Lock) -> None:
    """Yield until a waiter is parked on *lock*, so the verb has passed its first gate."""
    for _ in range(200):
        if getattr(lock, "_waiters", None):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the reload never queued on the slot lock")


def test_a_target_linked_while_queued_on_its_lock_is_refused(tmp_path, eager):
    """The gate re-runs after the slot-lock await. Mutation guard: authorizing
    once before the lock lets the teardown land on a channel-backed session."""
    state, caller, target = _world(tmp_path)

    async def _run():
        await target._lock.acquire()
        job = asyncio.create_task(
            sc.reload_target(state, caller_session_key=_key(caller), target="chat-2")
        )
        await _queued_on(target._lock)
        target.linked_session_key = "slack:1786300000.000100"
        target._lock.release()
        return await job

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(_run())

    assert exc.value.code == "linked_session_target"
    state.sessions.reset.assert_not_awaited()
    eager.assert_not_called()


def test_a_target_replaced_while_queued_on_its_lock_is_refused(tmp_path, eager):
    state, caller, target = _world(tmp_path)

    async def _run():
        await target._lock.acquire()
        job = asyncio.create_task(
            sc.reload_target(state, caller_session_key=_key(caller), target="chat-2")
        )
        await _queued_on(target._lock)
        replacement = MagicMock()
        state._slots["chat-2"] = replacement
        target._lock.release()
        return await job

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(_run())

    assert exc.value.code == "target_replaced"
    state.sessions.reset.assert_not_awaited()


def test_a_target_linked_during_the_reset_is_reported_and_audited(tmp_path, eager, audits):
    """The post-reset gate refusal must not escape as a plain refusal. Mutation
    guard: letting it propagate reports linked_session_target ("nothing
    happened") for a process that is already gone, and skips the audit."""
    state, caller, target = _world(tmp_path)
    before = list(target.messages)

    async def _reset_then_link(_session_key, **_kw):
        target.linked_session_key = "slack:1786300000.000100"
        return True

    state.sessions.reset = AsyncMock(side_effect=_reset_then_link)

    err = _refused(state, caller)

    assert err.code == "target_changed_during_reload"
    assert "linked_session_target" in str(err)
    state.sessions.reset.assert_awaited_once()
    assert target.messages == before
    eager.assert_not_called()
    assert audits[-1]["outcome"] == "denied"
    assert audits[-1]["detail"]["code"] == "target_changed_during_reload"
    assert audits[-1]["detail"]["gate_code"] == "linked_session_target"


def test_a_target_linked_during_the_children_probe_is_refused(tmp_path, monkeypatch, eager):
    """The gate re-runs after the in-lock children probe, which is an await.
    Mutation guard: without that re-check a channel link landing during the
    probe reaches the teardown on authorization read before it."""
    state, caller, target = _world(tmp_path)
    calls = []

    async def _link_on_second_probe(*_a, **_kw):
        # The first call is the verb's own pre-lock probe; the second is the
        # teardown's, inside both locks.
        calls.append(1)
        if len(calls) == 2:
            target.linked_session_key = "slack:1786300000.000100"
        return None

    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", _link_on_second_probe)
    before = list(target.messages)

    err = _refused(state, caller)

    assert len(calls) == 2
    assert err.code == "linked_session_target"
    state.sessions.reset.assert_not_awaited()
    assert target.messages == before
    eager.assert_not_called()


def test_a_target_replaced_during_the_reset_is_reported_and_audited(tmp_path, eager, audits):
    """A slot swap after the reset is the same "process gone, no notice" state
    as a post-reset gate refusal. Mutation guard: answering target_replaced
    ("not reloaded") misreports a torn-down process and audits the wrong code."""
    state, caller, target = _world(tmp_path)
    before = list(target.messages)

    async def _reset_then_replace(_session_key, **_kw):
        state._slots["chat-2"] = MagicMock()
        return True

    state.sessions.reset = AsyncMock(side_effect=_reset_then_replace)

    err = _refused(state, caller)

    assert err.code == "target_changed_during_reload"
    state.sessions.reset.assert_awaited_once()
    assert target.messages == before
    eager.assert_not_called()
    assert audits[-1]["detail"]["code"] == "target_changed_during_reload"
    assert audits[-1]["detail"]["gate_code"] == "slot_replaced"


# ── Teardown failures ────────────────────────────────────────────────────────


def test_a_shutdown_raise_after_the_pop_still_completes_the_reload(tmp_path, eager, audits):
    """SessionManager.reset pops the session before its shutdown can raise.
    Mutation guard: letting that raise escape answers 500 with no notice, no
    respawn and no audit for a process that is already gone."""
    state, caller, target = _world(tmp_path)

    async def _pop_then_raise(_session_key, **_kw):
        state.sessions.get_provider = MagicMock(return_value=None)
        raise RuntimeError("provider shutdown failed")

    state.sessions.reset = AsyncMock(side_effect=_pop_then_raise)

    out = _reload(state, caller)

    assert out["ok"] is True
    assert out["warning"] == chat_handlers._TEARDOWN_INCOMPLETE_WARNING
    assert target.messages[-1].get("meta", {}).get("kind") == chat_handlers.SESSION_RELOAD_KIND
    eager.assert_called_once()
    assert audits[-1]["outcome"] == "allowed"
    assert audits[-1]["detail"]["warning"] == chat_handlers._TEARDOWN_INCOMPLETE_WARNING


def test_a_raise_before_the_pop_is_reported_and_audited(tmp_path, eager, audits):
    """The same provider still registered means nothing was torn down.
    Mutation guard: an uncaught raise is a bare 500 with no audit."""
    state, caller, target = _world(tmp_path)
    before = list(target.messages)
    state.sessions.reset = AsyncMock(side_effect=RuntimeError("unblock failed"))

    err = _refused(state, caller)

    assert err.code == "reload_failed"
    assert err.status == 500
    assert target.messages == before
    eager.assert_not_called()
    assert audits[-1]["outcome"] == "denied"
    assert audits[-1]["detail"]["code"] == "reload_failed"


# ── Route ────────────────────────────────────────────────────────────────────


def _request(state, caller, *, internal: bool, body: dict):
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/reload"
    request.method = "POST"
    request.headers = {"X-Session-Key": _key(caller)}
    request.query = {}
    request.get = lambda key, default=None: (
        True if (key in ("internal_auth", "peer_verified") and internal) else default
    )

    async def _json():
        return body

    request.json = _json
    return request


def test_route_without_the_secret_is_forbidden(tmp_path, eager):
    state, caller, _target = _world(tmp_path)
    req = _request(state, caller, internal=False, body={"target": "chat-2"})

    resp = asyncio.run(handlers_sc.api_session_control_reload(req))

    assert resp.status == 403
    state.sessions.reset.assert_not_awaited()


def test_route_reloads_and_answers_the_target(tmp_path, eager):
    state, caller, _target = _world(tmp_path)
    req = _request(state, caller, internal=True, body={"target": "chat-2"})

    resp = asyncio.run(handlers_sc.api_session_control_reload(req))

    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": True, "target": "chat-2"}


def test_route_renders_busy_as_409(tmp_path, eager):
    state, caller, target = _world(tmp_path)
    _running(target)
    req = _request(state, caller, internal=True, body={"target": "chat-2"})

    resp = asyncio.run(handlers_sc.api_session_control_reload(req))

    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "target_busy"


def test_the_route_is_in_the_strict_internal_set():
    """Off this set the caller's X-Internal-Secret is ignored and the tool is
    unreachable in production."""
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/session-control/reload" in _STRICT_INTERNAL_API_PATHS


# ── MCP tool ─────────────────────────────────────────────────────────────────

_VERIFIED = "dashboard:chat-verified"


def _tool(name: str, args: dict, route: str, reply: dict) -> tuple[str, list[DashboardRequest]]:
    """One frame of ``name`` as the verified caller, against one dashboard route."""
    dash = InMemoryDashboardClient({route: reply})
    out = TABLE.call(name, args, ToolContext(dash, Caller.strict(_VERIFIED)))
    return out, dash.requests


def test_tool_carries_the_verified_key_and_reports_the_reload():
    out, (post,) = _tool(
        "session_reload",
        {"target": "chat-2"},
        "POST /api/session-control/reload",
        {"ok": True, "target": "chat-2"},
    )
    assert post.path == "/api/session-control/reload"
    assert post.body == {"target": "chat-2"}
    assert post.session_key == _VERIFIED
    assert "`chat-2` is relaunching its agent process" in out


def test_tool_reports_a_busy_refusal_as_an_error():
    out, _ = _tool(
        "session_reload",
        {"target": "chat-2"},
        "POST /api/session-control/reload",
        {"error": "session busy, not reloaded"},
    )
    assert out.startswith("Error:")
    assert "session busy, not reloaded" in out


def test_tool_reports_a_changed_target_as_reset_not_refused():
    """Mutation guard: the generic error branch tells the agent the reload
    did not happen for a process that was already torn down."""
    out, _ = _tool(
        "session_reload",
        {"target": "chat-2"},
        "POST /api/session-control/reload",
        {
            "error": "session changed during the reload",
            "code": "target_changed_during_reload",
        },
    )
    assert "could not reload" not in out
    assert "was reset" in out


def test_tool_reads_the_changed_target_code_before_any_error():
    """The code decides even on a reply with no ``error`` beside it.

    Mutation guard: checking the error first answers this as a plain reload.
    """
    out, _ = _tool(
        "session_reload",
        {"target": "chat-2"},
        "POST /api/session-control/reload",
        {"ok": True, "code": "target_changed_during_reload"},
    )
    assert "was reset" in out and "is relaunching" not in out


def test_tool_reports_a_degraded_teardown_as_a_completed_reload():
    out, _ = _tool(
        "session_reload",
        {"target": "chat-2"},
        "POST /api/session-control/reload",
        {
            "ok": True,
            "target": "chat-2",
            "warning": "old session teardown incomplete",
        },
    )
    assert "is relaunching" in out
    assert "old session teardown incomplete" in out


def test_tool_takes_no_agent_model_or_workspace():
    from kiro_crew.mcp_dashboard import _tool_definitions

    (tool,) = [t for t in _tool_definitions() if t["name"] == "session_reload"]
    assert set(tool["inputSchema"]["properties"]) == {"target"}
