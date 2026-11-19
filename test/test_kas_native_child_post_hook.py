"""A KAS native sub-agent's finished tool call fires the session agent's PostToolUse.

The session handle returns only an activity event for a child's nested tool
frame, so a turn loop never saw the child's tool result. On the FIRST terminal
frame of a child call the activity event now carries that call's outcome
(``acp.kas_child_finished``), and the dashboard turn, a channel turn and a
sub-agent run each fire PostToolUse on it exactly once, with the same redaction
and names as for the turn's own call. A frame that is not terminal fires nothing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_TOOL_RESULT,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.hooks import HOOK_EVENT_POST_TOOL_USE

_CHILD = {"kiro": {"agentSubtaskId": "sa-1"}}


def _handle() -> AcpSessionHandle:
    rt = MagicMock()
    rt.acp_backend = ACP_BACKEND_KAS
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return AcpSessionHandle("sB", asyncio.Queue(), rt)


def _update(handle: AcpSessionHandle, update: dict) -> list[AcpEvent]:
    return handle._handle_update(JsonRpcMessage(method="session/update", params={"update": update}))


def _call(handle: AcpSessionHandle, tcid: str = "child-tc") -> None:
    _update(
        handle,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": tcid,
            "title": "echo hi",
            "kind": "execute",
            "status": "in_progress",
            "rawInput": {"command": "echo hi"},
            "_meta": _CHILD,
        },
    )


def _finish(handle: AcpSessionHandle, tcid: str = "child-tc", status: str = "completed"):
    return _update(
        handle,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": tcid,
            "status": status,
            "rawOutput": {"stdout": "hi"},
            "_meta": _CHILD,
        },
    )


def _activity(events: list[AcpEvent]) -> AcpEvent:
    [event] = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
    assert not [e for e in events if e.kind == EVENT_TOOL_RESULT], "no top-level tool row"
    return event


# ── the session handle marks the finished call, once ─────────────────────────


def test_the_first_terminal_frame_of_a_child_call_carries_its_outcome() -> None:
    from kiro_crew.acp.kas_child_finished import is_finished_child_call

    handle = _handle()
    _call(handle)
    event = _activity(_finish(handle))
    assert is_finished_child_call(event)
    assert event.tool_status == "completed" and event.tool_final is True
    assert event.is_shell is True
    assert event.title == "echo hi"
    assert "hi" in event.tool_output


def test_a_repeated_terminal_frame_of_the_same_call_is_not_marked_again() -> None:
    from kiro_crew.acp.kas_child_finished import is_finished_child_call

    handle = _handle()
    _call(handle)
    assert is_finished_child_call(_activity(_finish(handle)))
    assert not is_finished_child_call(_activity(_finish(handle)))


@pytest.mark.parametrize("status", ["in_progress", "pending"])
def test_a_frame_that_is_not_terminal_carries_no_outcome(status: str) -> None:
    from kiro_crew.acp.kas_child_finished import is_finished_child_call

    handle = _handle()
    _call(handle)
    assert not is_finished_child_call(_activity(_finish(handle, status=status)))


def test_a_failed_child_call_is_marked_too() -> None:
    from kiro_crew.acp.kas_child_finished import is_finished_child_call

    handle = _handle()
    _call(handle)
    event = _activity(_finish(handle, status="failed"))
    assert is_finished_child_call(event) and event.tool_status == "failed"


def test_an_over_long_tool_call_id_is_not_remembered() -> None:
    from kiro_crew.acp import kas_child_finished

    handle = _handle()
    tcid = "x" * (kas_child_finished._KEY_MAX_CHARS + 1)
    _call(handle, tcid)
    assert not kas_child_finished.is_finished_child_call(_activity(_finish(handle, tcid)))
    assert handle._native_child_titles == {} and handle._native_child_finished == set()


def test_an_over_long_session_id_is_not_remembered_either() -> None:
    from kiro_crew.acp import kas_child_finished

    handle = _handle()
    handle._session_id = "s" * kas_child_finished._KEY_MAX_CHARS
    _call(handle)
    assert not kas_child_finished.is_finished_child_call(_activity(_finish(handle)))
    assert handle._native_child_titles == {} and handle._native_child_finished == set()


def test_a_kept_title_is_cut_to_its_bound() -> None:
    from kiro_crew.acp import kas_child_finished

    handle = _handle()
    _update(
        handle,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "child-tc",
            "title": "t" * 5000,
            "kind": "execute",
            "status": "in_progress",
            "_meta": _CHILD,
        },
    )
    assert all(
        len(v) <= kas_child_finished._TITLE_MAX_CHARS for v in handle._native_child_titles.values()
    )
    event = _activity(_finish(handle))
    assert len(event.title) <= kas_child_finished._TITLE_MAX_CHARS


# ── each turn loop fires PostToolUse on it ────────────────────────────────────


def _finished_event(output: str = "hi") -> AcpEvent:
    return AcpEvent(
        kind=EVENT_SUBAGENT_ACTIVITY,
        sub_session_id="sa-1",
        tool_call_id="child-tc",
        title="echo hi",
        tool_final=True,
        tool_status="completed",
        tool_output=output,
        is_shell=True,
    )


def test_the_dashboard_turn_fires_post_tool_use_once_with_the_shell_id(monkeypatch) -> None:
    from kiro_crew.dashboard import chat_runner

    fired: list[tuple[str, dict]] = []

    async def _record(turn, event, **kwargs):
        fired.append((event, kwargs))
        return []

    monkeypatch.setattr(chat_runner, "_fire", _record)
    turn = SimpleNamespace(
        client=SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS)), _pending_tool_ids={}
    )
    asyncio.run(chat_runner._on_child_tool_finished(turn, _finished_event()))
    unmarked = _finished_event()
    unmarked.tool_final, unmarked.tool_status = False, "in_progress"
    asyncio.run(chat_runner._on_child_tool_finished(turn, unmarked))
    assert len(fired) == 1
    event, kwargs = fired[0]
    assert event == HOOK_EVENT_POST_TOOL_USE
    assert kwargs["tool_name"] == "echo hi"
    assert kwargs["tool_response"] == {"output": "hi"}
    # The id a spec ``execute_bash`` matcher meets a KAS shell call by.
    from kiro_crew.acp.kas_permissions import KAS_TOOL_IDS_BY_KIRO_TOOL

    assert kwargs["tool_id"] == KAS_TOOL_IDS_BY_KIRO_TOOL["execute_bash"][0]


def test_the_dashboard_turn_redacts_the_output_as_for_its_own_call(monkeypatch) -> None:
    from kiro_crew.dashboard import chat_runner

    fired: list[dict] = []

    async def _record(turn, event, **kwargs):
        fired.append(kwargs)
        return []

    monkeypatch.setattr(chat_runner, "_fire", _record)
    from redaction_structured_cases import github_classic

    secret = github_classic(seed=11)
    turn = SimpleNamespace(
        client=SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS)), _pending_tool_ids={}
    )
    asyncio.run(chat_runner._on_child_tool_finished(turn, _finished_event(f"key {secret}")))
    assert fired and secret not in fired[0]["tool_response"]["output"]
    assert fired[0]["tool_response"]["output"] == chat_runner._redact_tool_field(f"key {secret}")


def test_a_channel_turn_fires_post_tool_use_once(monkeypatch) -> None:
    from kiro_crew.agent_sdk.spec_hooks import TurnSpecHooks
    from kiro_crew.messaging import tool_hooks

    store = SimpleNamespace(fire=AsyncMock(return_value=[]))
    monkeypatch.setattr(tool_hooks, "observer_store", lambda _turn, _spec: store)
    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), session_id="s")
    hooks = tool_hooks.ChannelTurnHooks(
        provider,
        SimpleNamespace(session_key="slack:t", store_hooks=True),
        TurnSpecHooks([], None, False, False),
    )
    asyncio.run(hooks.on_child_result(_finished_event()))
    unmarked = _finished_event()
    unmarked.tool_final, unmarked.tool_status = False, ""
    asyncio.run(hooks.on_child_result(unmarked))
    assert store.fire.await_count == 1
    args, kwargs = store.fire.await_args
    assert args[0] == HOOK_EVENT_POST_TOOL_USE
    assert kwargs["tool_name"] == "echo hi"
    assert kwargs["tool_response"] == {"output": "hi"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")
async def test_a_sub_agent_run_fires_post_tool_use_once_for_its_native_child() -> None:
    """A Kiro Crew sub-agent run whose own agent starts a native child."""
    from subagent_helpers import _mock_ctx_builder_auto_spawn, _mock_sessions

    from kiro_crew.execution_context import execution_for_store
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    async def _stream(*_a, **_kw):  # type: ignore[no-untyped-def]
        yield _finished_event()
        unmarked = _finished_event()
        unmarked.tool_call_id, unmarked.tool_final, unmarked.tool_status = "other", False, ""
        yield unmarked

    sessions = _mock_sessions()
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    ctx = _mock_ctx_builder_auto_spawn()
    ctx.hooks.auto_approve_subagent_tools = True
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
    manager.hook_store = MagicMock()
    manager.hook_store.fire = AsyncMock()
    info = SubagentInfo(
        execution_context=execution_for_store(""),
        id="t02",
        task="test",
        parent_session_key="dashboard:p",
    )
    manager._log_spawned(info)

    from unittest.mock import patch

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        await manager._run_inner(info, "subagent:t02")

    post = [c for c in manager.hook_store.fire.await_args_list if c.args[0] == "PostToolUse"]
    assert len(post) == 1
    assert post[0].kwargs["tool_name"] == "echo hi"
    assert post[0].kwargs["tool_response"] == {"output": "hi"}


def test_the_dashboard_keeps_the_name_the_childs_permission_request_gave(monkeypatch) -> None:
    """KAS names a child's read only on its permission request; its finish keeps that id."""
    from kiro_crew.agent_sdk.spec_hooks import PostToolIdentity
    from kiro_crew.dashboard import chat_runner

    fired: list[dict] = []

    async def _record(turn, event, **kwargs):
        fired.append(kwargs)
        return []

    monkeypatch.setattr(chat_runner, "_fire", _record)
    event = _finished_event()
    event.title, event.is_shell = "Read File", False
    turn = SimpleNamespace(
        client=SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS)),
        _pending_tool_ids={"child-tc": PostToolIdentity(tool_id="read_file")},
    )
    asyncio.run(chat_runner._on_child_tool_finished(turn, event))
    assert fired and fired[0]["tool_id"] == "read_file"
    assert "child-tc" not in turn._pending_tool_ids
