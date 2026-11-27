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
        return await _turn(rt, chat_a, stdout, _turn_with(running, _PROMPT, *on_parent, _DONE))

    events = _run(tmp_path, scenario)
    activity = [
        e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY and e.sub_session_id == CHILD
    ]
    assert [e.tool_call_id for e in activity if e.tool_call_id] == ["toolu_kept"]
    assert "child on parent" in "".join(e.text or "" for e in activity)
    assert not [e for e in events if e.kind == EVENT_TEXT_CHUNK and "child" in (e.text or "")]


_JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


@pytest.mark.parametrize(
    ("title", "leaked"),
    [
        (f"curl -H 'Authorization: Bearer {_JWT}'", _JWT),
        # A token across offset 4096 of a long title: no cut runs before the redactor.
        ("x " * 2040 + f"Bearer {_JWT} done", _JWT[:8]),
        ("Read src/app.py", None),
        ("搜索代码", None),
    ],
)
def test_a_childs_tool_title_on_the_parents_session_is_redacted(tmp_path, title, leaked):
    running = _announce({"state": "running"})
    stamp = {"_meta": {"claudeCode": {"parentToolUseId": AGENT_CALL}}}
    call = _update(
        "s-a",
        {"sessionUpdate": "tool_call", "toolCallId": "toolu_titled", "title": title, **stamp},
    )

    async def scenario(rt, chat_a, chat_b, stdout):
        return await _turn(rt, chat_a, stdout, _turn_with(running, _PROMPT, call, _DONE))

    events = _run(tmp_path, scenario)
    marks = [
        e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY and e.tool_call_id == "toolu_titled"
    ]
    assert len(marks) == 1
    if leaked is None:
        assert marks[0].title == title  # plain titles pass through unchanged
    else:
        assert leaked not in marks[0].title


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


def test_a_finished_child_is_not_counted_resident_on_a_later_turn(tmp_path):
    """The kept child ids are not the count: a later turn with no child running
    reports none, as the per-chat client does."""

    class _Budget:
        def __init__(self) -> None:
            self.counts: list[int] = []

        def report_uncharged(self, _kind: str, count: int, *, label: str = "") -> None:
            self.counts.append(count)

    async def scenario(rt, chat_a, chat_b, stdout):
        budget = _Budget()
        await _turn(rt, chat_a, stdout, _turn_with(_announce({"state": "running"}), _PROMPT, _DONE))
        during = chat_a.report_native_children(budget)
        await _turn(rt, chat_a, stdout, _answer())
        return during, chat_a.report_native_children(budget), set(chat_a._native_child_sids)

    during, later, kept = _run(tmp_path, scenario)
    assert during == 1 and later == 0 and kept == {CHILD}


@pytest.mark.parametrize("first_stop", ["end_turn", "max_tokens"])
def test_a_working_childs_credential_split_across_parent_turns_stays_masked(tmp_path, first_stop):
    """A background child's text runs through one redactor for its whole life: a
    connection URI its chunks split across two parent turns is read whole and
    masked, never shown in the clear because the second half met a fresh one,
    whatever ended the first turn short of a cancel."""
    secret = "SuperSecret123"

    def chunk(text: str) -> dict[str, Any]:
        return _child(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
        )

    async def scenario(rt, chat_a, chat_b, stdout):
        frames = _turn_with(
            _announce({"state": "running"}), _PROMPT, chunk("db is postgresql://user:")
        )
        frames[-1] = {**frames[-1], "result": {"stopReason": first_stop}}
        first = await _turn(rt, chat_a, stdout, frames)
        second = await _turn(
            rt, chat_a, stdout, _turn_with(chunk(f"{secret}@db.example.com:5432/prod ok\n"))
        )
        done = await _turn(rt, chat_a, stdout, _turn_with(_DONE))
        return first + second + done

    events = _run(tmp_path, scenario)
    shown = "".join(
        e.text or ""
        for e in events
        if e.kind == EVENT_SUBAGENT_ACTIVITY and e.sub_session_id == CHILD
    )
    assert secret not in shown
    # The rest of the line still reaches the card; only the credential is masked.
    assert shown.rstrip("\n").endswith("5432/prod ok")


def test_a_question_no_session_owns_is_declined_not_left_waiting(tmp_path, monkeypatch):
    """A child no registered session owns (past the announcement cap, or never
    announced) asks a question: the reader declines and records it, as it
    answers such a child's permission request, instead of dropping it."""
    from unittest.mock import MagicMock

    from kiro_crew import sel as sel_module

    recorder = MagicMock()
    monkeypatch.setattr(sel_module, "sel", lambda: recorder)

    async def scenario(rt, chat_a, chat_b, stdout):
        rt.send_response = AsyncMock()  # type: ignore[method-assign]
        await _turn(rt, chat_a, stdout, _turn_with(_question("child-unowned", 701)))
        for task in list(rt._answer_tasks) + list(rt._audit_tasks):
            await task
        return rt.send_response.await_args_list

    sent = _run(tmp_path, scenario)
    assert [(call.args[0], call.args[1]["action"]) for call in sent] == [(701, "decline")]
    record = recorder.log_tool_invocation.call_args.kwargs
    assert record["tool_name"] == "mcp_elicitation"
    assert record["metadata"]["child_session_id"] == "child-unowned"


def test_a_child_that_finishes_between_parent_turns_is_settled_by_the_next(tmp_path):
    """A background child's ``idle`` update arrives while no turn reads: the next
    turn's start drain still settles its row, so its card stops showing working,
    a Stop does not name it, and its redactor goes."""
    running = _announce({"state": "running"}, capabilities={"cancel": {}})

    async def scenario(rt, chat_a, chat_b, stdout):
        from kiro_crew.acp.types import JsonRpcMessage

        await _turn(rt, chat_a, stdout, _turn_with(running, _PROMPT))
        before = chat_a._native_child_rows[CHILD]["status"]["type"]
        # Queued for chat A with no turn reading it.
        rt._session_queues["s-a"].put_nowait(JsonRpcMessage.from_dict(_DONE))
        await _turn(rt, chat_a, stdout, _answer())
        rt.send_notification = AsyncMock()  # type: ignore[method-assign]
        stopped = await AcpSessionProvider(chat_a, rt, session_key="a").cancel_native_child(CHILD)
        return before, chat_a._native_child_rows[CHILD]["status"]["type"], stopped

    before, after, stopped = _run(tmp_path, scenario)
    assert before == "working" and after != "working" and stopped is False


def test_a_child_ending_between_turns_keeps_the_text_its_redactor_held(tmp_path):
    """The text a working child's redactor still held when it ended between the
    parent's turns, and its final roster, reach its card at the next turn's head."""

    async def scenario(rt, chat_a, chat_b, stdout):
        from kiro_crew.acp.types import JsonRpcMessage

        held = _child(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "Result: see postgresql://user:"},
            }
        )
        await _turn(rt, chat_a, stdout, _turn_with(_announce({"state": "running"}), _PROMPT, held))
        rt._session_queues["s-a"].put_nowait(JsonRpcMessage.from_dict(_DONE))
        return await _turn(rt, chat_a, stdout, _answer())

    events = _run(tmp_path, scenario)
    shown = "".join(
        e.text or ""
        for e in events
        if e.kind == EVENT_SUBAGENT_ACTIVITY and e.sub_session_id == CHILD
    )
    assert shown.endswith("postgresql://user:")
    rosters = [e.subagents for e in events if e.kind == EVENT_SUBAGENT_LIST]
    assert rosters and rosters[0][0]["status"]["type"] == "completed"


def test_the_roster_a_between_turn_end_draws_names_only_that_child(tmp_path):
    """A child that ended in the last turn is not drawn again when another child's
    end is read between turns: the drain starts this turn's roster first."""
    other = "child-0002"

    def announce(sid: str, state: dict[str, Any]) -> dict[str, Any]:
        return _update(
            "s-a",
            {
                "sessionUpdate": "subagent_update",
                "sessionId": sid,
                "title": "general-purpose",
                "description": f"task of {sid}",
                "state": state,
            },
        )

    def prompt(sid: str, call: str) -> dict[str, Any]:
        return _update(
            sid,
            {
                "sessionUpdate": "session_message",
                "messageId": call,
                "content": [{"type": "text", "text": f"Do {sid}."}],
            },
        )

    async def scenario(rt, chat_a, chat_b, stdout):
        from kiro_crew.acp.types import JsonRpcMessage

        await _turn(
            rt,
            chat_a,
            stdout,
            _turn_with(
                announce(CHILD, {"state": "running"}),
                prompt(CHILD, "toolu_1"),
                announce(other, {"state": "running"}),
                prompt(other, "toolu_2"),
                announce(CHILD, {"state": "idle", "stopReason": "end_turn"}),
            ),
        )
        ended = announce(other, {"state": "idle", "stopReason": "end_turn"})
        rt._session_queues["s-a"].put_nowait(JsonRpcMessage.from_dict(ended))
        return await _turn(rt, chat_a, stdout, _answer())

    events = _run(tmp_path, scenario)
    rosters = [e.subagents for e in events if e.kind == EVENT_SUBAGENT_LIST]
    assert rosters
    assert {row["sessionId"] for roster in rosters for row in roster} == {other}
