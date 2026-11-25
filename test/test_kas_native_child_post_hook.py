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
    assert event.title == event.hook_subject == "echo hi"
    assert "hi" in event.tool_output


def test_a_generic_wire_title_carries_the_parsed_command() -> None:
    # A hook matching ``git*`` must see the command, as for the parent's own call.
    handle = _handle()
    _update(
        handle,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "child-tc",
            "title": "Run Command",
            "kind": "execute",
            "status": "in_progress",
            "rawInput": {"command": "git status"},
            "_meta": _CHILD,
        },
    )
    finished = _activity(_finish(handle))
    assert finished.title == finished.hook_subject == "git status"


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
        len(v) <= kas_child_finished._TITLE_KEPT_CHARS for v in handle._native_child_titles.values()
    )
    event = _activity(_finish(handle))
    assert len(event.title) <= kas_child_finished._TITLE_KEPT_CHARS


# ── each turn loop fires PostToolUse on it ────────────────────────────────────


def _finished_event(output: str = "hi") -> AcpEvent:
    return AcpEvent(
        kind=EVENT_SUBAGENT_ACTIVITY,
        sub_session_id="sa-1",
        tool_call_id="child-tc",
        title="echo hi",
        hook_subject="echo hi",
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
    assert kwargs["tool_response"] == {"output": "hi", "truncated": False, "output_length": 2}
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
    assert kwargs["tool_response"] == {"output": "hi", "truncated": False, "output_length": 2}


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")
async def test_a_sub_agent_run_fires_post_tool_use_once_for_its_native_child(monkeypatch) -> None:
    """A Kiro Crew sub-agent run whose own agent starts a native child."""
    from subagent_helpers import _mock_ctx_builder_auto_spawn, _mock_sessions

    from kiro_crew.execution_context import execution_for_store
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    async def _stream(*_a, **_kw):  # type: ignore[no-untyped-def]
        yield _finished_event()
        unmarked = _finished_event()
        unmarked.tool_call_id, unmarked.tool_final, unmarked.tool_status = "other", False, ""
        yield unmarked

    from kiro_crew.agent_sdk import kas_child_finished as sdk_child

    # A full KAS sub-agent run double is out of scope here: the run is read as a
    # KAS session at the finished-child check only.
    kas = capabilities_for(ACP_BACKEND_KAS)
    monkeypatch.setattr(sdk_child, "capabilities_of", lambda _provider: kas)
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
    assert post[0].kwargs["tool_response"] == {
        "output": "hi",
        "truncated": False,
        "output_length": 2,
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")
@pytest.mark.parametrize("long", [False, True], ids=["short", "past-the-display-cut"])
async def test_a_sub_agent_run_meets_a_git_hook_with_the_childs_command(monkeypatch, long) -> None:
    from subagent_helpers import _mock_ctx_builder_auto_spawn, _mock_sessions

    from kiro_crew.execution_context import execution_for_store
    from kiro_crew.hook_runtime.tool_identity import _tool_matches
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    command = _LONG if long else "git status"
    finished = _git_status_event(command)

    async def _stream(*_a, **_kw):  # type: ignore[no-untyped-def]
        yield finished

    from kiro_crew.agent_sdk import kas_child_finished as sdk_child

    # A full KAS sub-agent run double is out of scope here: the run is read as a
    # KAS session at the finished-child check only.
    kas = capabilities_for(ACP_BACKEND_KAS)
    monkeypatch.setattr(sdk_child, "capabilities_of", lambda _provider: kas)
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
        id="t03",
        task="test",
        parent_session_key="dashboard:p",
    )
    manager._log_spawned(info)

    from unittest.mock import patch

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        await manager._run_inner(info, "subagent:t03")

    post = [c for c in manager.hook_store.fire.await_args_list if c.args[0] == "PostToolUse"]
    assert [c.kwargs["tool_name"] for c in post] == [command]
    assert _tool_matches("*git status*", post[0].kwargs["tool_name"])


def _git_status_event(command: str = "git status") -> AcpEvent:
    """The finished activity event the session handle builds for a generic wire title."""
    handle = _handle()
    _update(
        handle,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "child-tc",
            "title": "Run Command",
            "kind": "execute",
            "status": "in_progress",
            "rawInput": {"command": command},
            "_meta": _CHILD,
        },
    )
    return _activity(_finish(handle))


def test_each_turn_loop_meets_a_git_hook_with_the_childs_command(monkeypatch) -> None:
    """A saved ``git*`` PostToolUse matcher meets the child's call as the parent's."""
    from kiro_crew.agent_sdk.spec_hooks import TurnSpecHooks
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.hook_runtime.tool_identity import _tool_matches
    from kiro_crew.messaging import tool_hooks

    names: list[str] = []

    async def _record(turn, event, **kwargs):
        names.append(kwargs["tool_name"])
        return []

    monkeypatch.setattr(chat_runner, "_fire", _record)
    turn = SimpleNamespace(
        client=SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS)), _pending_tool_ids={}
    )
    asyncio.run(chat_runner._on_child_tool_finished(turn, _git_status_event()))

    store = SimpleNamespace(fire=AsyncMock(return_value=[]))
    monkeypatch.setattr(tool_hooks, "observer_store", lambda _turn, _spec: store)
    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), session_id="s")
    hooks = tool_hooks.ChannelTurnHooks(
        provider,
        SimpleNamespace(session_key="slack:t", store_hooks=True),
        TurnSpecHooks([], None, False, False),
    )
    asyncio.run(hooks.on_child_result(_git_status_event()))
    names.append(store.fire.await_args.kwargs["tool_name"])

    assert names == ["git status", "git status"]
    assert all(_tool_matches("git*", n) for n in names)


def test_the_kept_command_title_is_the_redacted_form() -> None:
    from redaction_structured_cases import github_classic

    from kiro_crew.acp._dispatch import redact_text

    secret = github_classic(seed=12)
    title = _git_status_event(f"git push https://x:{secret}@example.com").title
    assert secret not in title
    assert title == redact_text(f"git push https://x:{secret}@example.com")


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
    event.title = event.hook_subject = "Read File"
    event.is_shell = False
    turn = SimpleNamespace(
        client=SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS)),
        _pending_tool_ids={"child-tc": PostToolIdentity(tool_id="read_file")},
    )
    asyncio.run(chat_runner._on_child_tool_finished(turn, event))
    assert fired and fired[0]["tool_id"] == "read_file"
    assert "child-tc" not in turn._pending_tool_ids


# ── only a KAS session's event is a finished child ───────────────────────────


@pytest.mark.parametrize("backend", ["", "claude", "codex", "opencode", "goose", "unknown-backend"])
def test_a_same_shaped_event_on_another_backend_is_not_a_finished_child(backend) -> None:
    from kiro_crew.agent_sdk.kas_child_finished import is_finished_child_call

    event = _finished_event()
    kas = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS))
    other = SimpleNamespace(capabilities=capabilities_for(backend))
    assert is_finished_child_call(event, kas)
    assert not is_finished_child_call(event, other)
    assert not is_finished_child_call(event, object())


def test_the_dashboard_and_channel_fire_nothing_for_another_backends_event(monkeypatch) -> None:
    from kiro_crew.agent_sdk.spec_hooks import TurnSpecHooks
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.messaging import tool_hooks

    fired: list[str] = []

    async def _record(turn, event, **kwargs):
        fired.append(event)
        return []

    monkeypatch.setattr(chat_runner, "_fire", _record)
    turn = SimpleNamespace(
        client=SimpleNamespace(capabilities=capabilities_for("claude")), _pending_tool_ids={}
    )
    asyncio.run(chat_runner._on_child_tool_finished(turn, _finished_event()))
    store = SimpleNamespace(fire=AsyncMock(return_value=[]))
    monkeypatch.setattr(tool_hooks, "observer_store", lambda _turn, _spec: store)
    provider = SimpleNamespace(capabilities=capabilities_for("claude"), session_id="s")
    hooks = tool_hooks.ChannelTurnHooks(
        provider,
        SimpleNamespace(session_key="slack:t", store_hooks=True),
        TurnSpecHooks([], None, False, False),
    )
    asyncio.run(hooks.on_child_result(_finished_event()))
    assert fired == [] and store.fire.await_count == 0


# ── the hook subject is the call's own, uncut; the display title is cut ──────

#: A command whose matching part sits past the display cut.
_LONG = "echo " + "x" * 600 + " && git status"


def _long_event(command: str = _LONG) -> AcpEvent:
    return _git_status_event(command)


def test_a_long_childs_command_keeps_its_whole_subject_and_a_cut_title() -> None:
    from kiro_crew.acp import kas_child_finished

    # Longer than the title's kept bound, so the title is cut and the subject is not.
    longer = "echo " + "x" * 6000 + " && git status"
    event = _long_event(longer)
    assert event.hook_subject == longer
    assert len(event.title) == kas_child_finished._TITLE_KEPT_CHARS
    assert "git status" not in event.title


def _dashboard_name(monkeypatch, event: AcpEvent) -> str:
    from kiro_crew.dashboard import chat_runner

    names: list[str] = []

    async def _record(turn, ev, **kwargs):
        names.append(kwargs["tool_name"])
        return []

    monkeypatch.setattr(chat_runner, "_fire", _record)
    turn = SimpleNamespace(
        client=SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS)), _pending_tool_ids={}
    )
    asyncio.run(chat_runner._on_child_tool_finished(turn, event))
    [name] = names
    return name


def _channel_name(monkeypatch, event: AcpEvent) -> str:
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
    asyncio.run(hooks.on_child_result(event))
    return store.fire.await_args.kwargs["tool_name"]


def test_the_dashboard_and_channel_meet_a_hook_past_the_display_cut(monkeypatch) -> None:
    from kiro_crew.hook_runtime.tool_identity import _tool_matches

    names = [_dashboard_name(monkeypatch, _long_event()), _channel_name(monkeypatch, _long_event())]
    assert names == [_LONG, _LONG]
    assert all(_tool_matches("*git status*", n) for n in names)


def test_the_parents_same_long_command_is_matched_by_the_same_string() -> None:
    """The control: the parent's own call reaches its hook by the parser's title,
    which is the string the child's subject now is."""
    handle = _handle()
    [own] = [
        e
        for e in _update(
            handle,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "own-tc",
                "title": "Run Command",
                "kind": "execute",
                "status": "in_progress",
                "rawInput": {"command": _LONG},
            },
        )
        if e.kind == "tool_call"
    ]
    assert own.title == _long_event().hook_subject == _LONG


def test_a_secret_in_the_childs_command_reaches_the_hook_redacted(monkeypatch) -> None:
    from redaction_structured_cases import github_classic

    from kiro_crew.acp._dispatch import redact_text

    secret = github_classic(seed=13)
    command = "echo " + "x" * 600 + f" && git push https://x:{secret}@example.com"
    name = _dashboard_name(monkeypatch, _long_event(command))
    assert secret not in name
    assert name == redact_text(command)


def test_a_call_whose_subject_was_not_kept_is_never_matched_by_the_cut_title(monkeypatch) -> None:
    """Past the entry bound no subject is kept; the hook then meets the finished
    frame's own uncut title, here none, never the cut display text."""
    from kiro_crew.acp import kas_child_finished
    from kiro_crew.hook_runtime.tool_identity import _tool_matches

    monkeypatch.setattr(kas_child_finished, "_FINISHED_CAP", 0)
    handle = _handle()
    _update(
        handle,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "child-tc",
            "title": "Run Command",
            "kind": "execute",
            "status": "in_progress",
            "rawInput": {"command": _LONG},
            "_meta": _CHILD,
        },
    )
    assert handle._native_child_subjects == {}
    event = _activity(
        _update(
            handle,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "child-tc",
                "status": "completed",
                "rawOutput": {"stdout": "hi"},
                "_meta": _CHILD,
            },
        )
    )
    # Nothing was kept and this frame states no title: the subject is empty, so a
    # ``*git status*`` hook does not run for it (a ``*`` hook still does). It is
    # never the cut display text.
    assert event.hook_subject == ""
    assert not _tool_matches("*git status*", event.hook_subject)
    assert _tool_matches("*", event.hook_subject)


def test_the_provider_forwards_the_childs_hook_subject() -> None:
    """Every turn loop reads the provider's event, so the subject must survive it."""
    from kiro_crew.providers.acp_turns import _to_llm_event

    event = _long_event()
    forwarded = _to_llm_event(event)
    assert forwarded.hook_subject == _LONG
    assert forwarded.title == event.title


def test_a_subject_over_its_bound_is_not_kept_and_never_cut(monkeypatch) -> None:
    from kiro_crew.acp import kas_child_finished

    monkeypatch.setattr(kas_child_finished, "_SUBJECT_MAX_CHARS", 100)
    handle = _handle()
    _update(
        handle,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "child-tc",
            "title": "Run Command",
            "kind": "execute",
            "status": "in_progress",
            "rawInput": {"command": _LONG},
            "_meta": _CHILD,
        },
    )
    assert handle._native_child_subjects == {}
    event = _activity(_finish(handle))
    assert event.hook_subject == ""
    # Within the bound it is kept whole.
    monkeypatch.setattr(kas_child_finished, "_SUBJECT_MAX_CHARS", len(_LONG))
    assert _long_event().hook_subject == _LONG
