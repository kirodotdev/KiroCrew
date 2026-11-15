"""Claude Code's Agent-tool children, per session on one shared claude-agent-acp process.

claude-agent-acp announces a child on its parent's session (``subagent_update``) and
sends the child's work under the child's own sessionId. The runtime's reader learns the
child's owner from the announcement and routes its frames there; the handle draws the
child's card (``session_handle_claude_children``). Each test runs chat A's child while
chat B shares the process, and checks that A's card gets it and B sees none of it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from test_claude_shared_runtime import harness  # noqa: F401  (fixture, by name)
from test_claude_shared_surfaces import _answer, _run, _turn, _update

from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    METHOD_CANCEL,
)

pytestmark = pytest.mark.usefixtures("harness")

CHILD = "child-0001"
AGENT_CALL = "toolu_agent"


def _announce(state: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return _update(
        "s-a",
        {
            "sessionUpdate": "subagent_update",
            "sessionId": CHILD,
            "title": "general-purpose",
            "description": "Count the files",
            "state": state,
            **fields,
        },
    )


def _child(update: dict[str, Any]) -> dict[str, Any]:
    return _update(CHILD, update)


_PROMPT = _child(
    {
        "sessionUpdate": "session_message",
        "messageId": AGENT_CALL,
        "content": [{"type": "text", "text": "Count the files."}],
    }
)
_TEXT = _child(
    {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "child says hi"}}
)
_CALL = _child(
    {
        "sessionUpdate": "tool_call",
        "toolCallId": "toolu_child_ls",
        "title": "ls",
        "kind": "execute",
        "status": "pending",
        "rawInput": {"command": "ls"},
    }
)
_DONE = _announce({"state": "idle", "stopReason": "end_turn"})


def _turn_with(*frames: dict[str, Any]) -> list[dict[str, Any]]:
    return [*frames, *_answer()]


def test_a_childs_card_and_work_reach_only_its_own_chat(tmp_path):
    running = _announce({"state": "running"})

    async def scenario(rt, chat_a, chat_b, stdout):
        events_a = await _turn(
            rt, chat_a, stdout, _turn_with(running, _PROMPT, _TEXT, _CALL, _DONE)
        )
        events_b = await _turn(rt, chat_b, stdout, _answer())
        return events_a, events_b

    events_a, events_b = _run(tmp_path, scenario)
    rosters = [e.subagents for e in events_a if e.kind == EVENT_SUBAGENT_LIST]
    assert rosters and [row["sessionId"] for row in rosters[-1]] == [CHILD]
    assert rosters[-1][0]["initialQuery"] == "Count the files"
    assert rosters[-1][0]["status"]["type"] == "completed"
    activity = [e for e in events_a if e.kind == EVENT_SUBAGENT_ACTIVITY]
    assert "child says hi" in "".join(e.text or "" for e in activity if e.sub_session_id == CHILD)
    assert [e.tool_call_id for e in activity if e.tool_call_id] == ["toolu_child_ls"]
    # The child's work is its card's, never the parent's transcript.
    assert not [e for e in events_a if e.kind == EVENT_TEXT_CHUNK and "child" in (e.text or "")]
    assert not [e for e in events_a if e.kind == EVENT_TOOL_CALL]
    assert not [e for e in events_b if e.kind in (EVENT_SUBAGENT_LIST, EVENT_SUBAGENT_ACTIVITY)]


def test_a_childs_route_belongs_to_the_chat_that_announced_it(tmp_path):
    running = _announce({"state": "running"})

    async def scenario(rt, chat_a, chat_b, stdout):
        await _turn(rt, chat_a, stdout, _turn_with(running, _PROMPT))
        return dict(rt._announced_children)

    assert _run(tmp_path, scenario) == {CHILD: "s-a"}


def test_a_stop_reaches_only_the_child_of_the_chat_that_asks(tmp_path):
    running = _announce({"state": "running"}, capabilities={"cancel": {}})

    async def scenario(rt, chat_a, chat_b, stdout):
        await _turn(rt, chat_a, stdout, _turn_with(running, _PROMPT))
        rt.send_notification = AsyncMock()  # type: ignore[method-assign]
        provider_a = AcpSessionProvider(chat_a, rt, session_key="a")
        provider_b = AcpSessionProvider(chat_b, rt, session_key="b")
        stopped = (
            await provider_b.cancel_native_child(CHILD),
            await provider_a.cancel_native_child(CHILD),
        )
        return stopped, rt.send_notification.await_args_list

    stopped, sent = _run(tmp_path, scenario)
    assert stopped == (False, True)
    assert [call.args for call in sent] == [(METHOD_CANCEL, {"sessionId": CHILD})]


def test_a_child_granted_no_cancel_is_not_stopped(tmp_path):
    running = _announce({"state": "running"})

    async def scenario(rt, chat_a, chat_b, stdout):
        await _turn(rt, chat_a, stdout, _turn_with(running, _PROMPT))
        rt.send_notification = AsyncMock()  # type: ignore[method-assign]
        stopped = await AcpSessionProvider(chat_a, rt, session_key="a").cancel_native_child(CHILD)
        return stopped, rt.send_notification.await_count

    assert _run(tmp_path, scenario) == (False, 0)


def test_a_childs_permission_request_is_kept_for_its_stop_in_its_own_chat(tmp_path):
    running = _announce({"state": "running"}, capabilities={"cancel": {}})
    ask = {
        "jsonrpc": "2.0",
        "id": 501,
        "method": "session/request_permission",
        "params": {
            "sessionId": CHILD,
            "toolCall": {"toolCallId": "toolu_child_ls", "title": "ls", "kind": "execute"},
            "options": [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
            ],
        },
    }

    async def scenario(rt, chat_a, chat_b, stdout):
        rt.send_response = AsyncMock()  # type: ignore[method-assign]
        await _turn(rt, chat_a, stdout, _turn_with(running, _PROMPT, _CALL, ask))
        provider_a, provider_b = (
            AcpSessionProvider(chat, rt, session_key=key)
            for chat, key in ((chat_a, "a"), (chat_b, "b"))
        )
        return provider_a.native_child_requests(CHILD), provider_b.native_child_requests(CHILD)

    assert _run(tmp_path, scenario) == (("501",), ())


def test_a_childs_frames_on_the_parents_session_go_on_its_card(tmp_path):
    """The adapter keeps a child's tool call on the parent's session once a request
    made it there, stamped with the Agent call it serves: still the child's work."""
    running = _announce({"state": "running"})
    stamp = {"_meta": {"claudeCode": {"parentToolUseId": AGENT_CALL}}}
    on_parent = [
        _update(
            "s-a",
            {"sessionUpdate": "tool_call", "toolCallId": "toolu_kept", "title": "cat", **stamp},
        ),
        _update("s-a", {"sessionUpdate": "tool_call_update", "toolCallId": "toolu_kept", **stamp}),
        _update(
            "s-a",
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "child on parent"},
                **stamp,
            },
        ),
    ]

    async def scenario(rt, chat_a, chat_b, stdout):
        return await _turn(rt, chat_a, stdout, _turn_with(running, _PROMPT, *on_parent))

    events = _run(tmp_path, scenario)
    activity = [
        e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY and e.sub_session_id == CHILD
    ]
    assert [e.tool_call_id for e in activity if e.tool_call_id] == ["toolu_kept"]
    assert "child on parent" in "".join(e.text or "" for e in activity)
    assert not [e for e in events if e.kind == EVENT_TEXT_CHUNK and "child" in (e.text or "")]


def test_a_stop_that_cannot_be_sent_reports_the_child_not_stopped(tmp_path):
    running = _announce({"state": "running"}, capabilities={"cancel": {}})

    async def scenario(rt, chat_a, chat_b, stdout):
        await _turn(rt, chat_a, stdout, _turn_with(running, _PROMPT))
        rt.send_notification = AsyncMock(side_effect=OSError("pipe closed"))  # type: ignore[method-assign]
        return await AcpSessionProvider(chat_a, rt, session_key="a").cancel_native_child(CHILD)

    assert _run(tmp_path, scenario) is False


def test_a_turns_start_says_last_turns_refusals_once(tmp_path, caplog):
    async def scenario(rt, chat_a, chat_b, stdout):
        chat_a._native_child_overflow = 2
        chat_a._reset_claude_child_turn()
        return chat_a._native_child_overflow

    with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
        assert _run(tmp_path, scenario) == 0
    assert "made 2 refusal(s)" in caplog.text


def test_a_child_that_outlived_a_turn_is_read_on_its_first_frame_of_the_next(tmp_path):
    """The next turn's start clears the turn's child ids, not the child's row: its
    first frame there, a notice, still goes on its card and lists it again."""
    running = _announce({"state": "running"})
    notice = _child({"sessionUpdate": "notice", "title": "Rate limited", "description": "retrying"})

    async def scenario(rt, chat_a, chat_b, stdout):
        await _turn(rt, chat_a, stdout, _turn_with(running, _PROMPT))
        return await _turn(rt, chat_a, stdout, _turn_with(notice))

    events = _run(tmp_path, scenario)
    assert [e for e in events if e.kind == EVENT_SUBAGENT_LIST]
    activity = [
        e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY and e.sub_session_id == CHILD
    ]
    assert "Rate limited" in "".join(e.text or "" for e in activity)


def test_a_child_call_with_no_agent_call_link_is_still_marked_a_childs(tmp_path):
    """A call on the parent's session stamped with an Agent call this session has no
    link for (refused at its cap) is marked as a child's, as on the per-chat client,
    so a session directive it carries cannot act on the parent's chat."""
    call = _update(
        "s-a",
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "toolu_unlinked",
            "title": "set_project",
            "kind": "other",
            "status": "pending",
            "_meta": {"claudeCode": {"parentToolUseId": "toolu_unknown_agent"}},
        },
    )

    async def scenario(rt, chat_a, chat_b, stdout):
        await _turn(rt, chat_a, stdout, _turn_with(_announce({"state": "running"})))
        return await _turn(rt, chat_a, stdout, _turn_with(call))

    events = _run(tmp_path, scenario)
    marks = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
    assert [(e.sub_session_id, e.tool_call_id) for e in marks] == [
        ("toolu_unknown_agent", "toolu_unlinked")
    ]


def test_a_claude_childs_id_and_last_turns_refusals_outlive_the_turn_start(tmp_path):
    """The per-turn reset keeps claude child ids: they gate rows kept for the session's
    life, as on the per-chat client, and the refusal count is said, then cleared."""

    async def scenario(rt, chat_a, chat_b, stdout):
        await _turn(rt, chat_a, stdout, _turn_with(_announce({"state": "running"}), _PROMPT))
        chat_a._native_child_overflow = 3
        await _turn(rt, chat_a, stdout, _answer())
        return set(chat_a._native_child_sids), chat_a._native_child_overflow

    sids, overflow = _run(tmp_path, scenario)
    assert sids == {CHILD} and overflow == 0


def _question(sid: str, rid: int) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "elicitation/create",
        "params": {
            "sessionId": sid,
            "message": "Which folder?",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }


def test_a_childs_question_reaches_its_own_chats_turn(tmp_path):
    """An announced child's ``elicitation/create`` routes to the chat that announced
    it while that chat's turn reads, never to the other chat."""

    async def scenario(rt, chat_a, chat_b, stdout):
        routed: list[str] = []
        real = rt._session_queues["s-a"].put

        async def spy(msg):
            if msg is not None and msg.method == "elicitation/create":
                routed.append(str((msg.params or {}).get("sessionId")))
            await real(msg)

        rt._session_queues["s-a"].put = spy
        rt.send_response = AsyncMock()  # type: ignore[method-assign]
        await _turn(
            rt,
            chat_a,
            stdout,
            _turn_with(_announce({"state": "running"}), _PROMPT, _question(CHILD, 601)),
        )
        return routed

    assert _run(tmp_path, scenario) == [CHILD]


def test_a_childs_question_with_no_turn_reading_is_declined(tmp_path, monkeypatch):
    """Declined, and the decision recorded under the elicitation audit operation,
    keyed on the owner and naming the child, as every elicitation decision is."""
    from unittest.mock import MagicMock

    from kiro_crew import sel as sel_module

    recorder = MagicMock()
    monkeypatch.setattr(sel_module, "sel", lambda: recorder)

    async def scenario(rt, chat_a, chat_b, stdout):
        await _turn(rt, chat_a, stdout, _turn_with(_announce({"state": "running"}), _PROMPT))
        rt.send_response = AsyncMock()  # type: ignore[method-assign]
        from kiro_crew.acp.types import JsonRpcMessage

        await rt._route_owned_child_frame(
            JsonRpcMessage.from_dict(_question(CHILD, 602)), CHILD, "s-a"
        )
        for task in list(rt._answer_tasks):
            await task
        for task in list(rt._audit_tasks):
            await task
        return rt.send_response.await_args_list

    sent = _run(tmp_path, scenario)
    assert [call.args[0] for call in sent] == [602]
    assert sent[0].args[1]["action"] == "decline"
    record = recorder.log_tool_invocation.call_args.kwargs
    assert record["tool_name"] == "mcp_elicitation" and record["outcome"] == "decline"
    assert record["session_key"] == "acp:s-a" and record["request_id"] == 602
    assert record["metadata"] == {"reason": "owner_no_active_turn", "child_session_id": CHILD}


def test_a_childs_declined_question_waits_on_the_shared_answer_bound(tmp_path):
    """The decline joins the one answer-task set under its bound: with no room it
    spawns nothing, as a permission answer does."""

    async def scenario(rt, chat_a, chat_b, stdout):
        await _turn(rt, chat_a, stdout, _turn_with(_announce({"state": "running"}), _PROMPT))
        rt.send_response = AsyncMock()  # type: ignore[method-assign]
        asked: list[str] = []

        async def full(_msg, *, request_kind, **_kw):
            asked.append(request_kind)
            return False

        rt._wait_for_answer_capacity = full  # type: ignore[method-assign]
        from kiro_crew.acp.types import JsonRpcMessage

        await rt._route_owned_child_frame(
            JsonRpcMessage.from_dict(_question(CHILD, 603)), CHILD, "s-a"
        )
        return asked, set(rt._answer_tasks), rt.send_response.await_count

    asked, tasks, sent = _run(tmp_path, scenario)
    assert asked == ["child question"] and not tasks and sent == 0
