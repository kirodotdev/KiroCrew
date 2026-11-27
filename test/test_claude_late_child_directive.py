"""A Claude child's tool call the adapter names as a child's only on a later update.

claude-agent-acp emits a tool call eagerly, on the session that asked for it, when
Claude Code asks permission for it before the Agent call that spawned its subagent
has streamed (``ensureToolCallEmitted``). That first ``tool_call`` frame names no
parent. The streamed ``tool_use`` then refines the call with a ``tool_call_update``
stamped ``_meta.claudeCode.parentToolUseId``, or routed to the child's own session.
The call is the child's from that update on, so it must be marked as the child's
there too: the dashboard refuses a session directive only from a call a child mark
names, and an unmarked child call would claim the record parked under the parent's
session and apply the child's directive to the parent's chat.

The frames go through the real per-chat ``AcpClient`` and the shared runtime's
session handle, and the per-chat client's events through the dashboard turn with a
record parked as the directive tool parks it, so the test follows the whole path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_claude_native_subagent_cards import _client, _turn, _update
from test_claude_shared_runtime import harness  # noqa: F401  (fixture, by name)
from test_claude_shared_surfaces import _answer, _run
from test_claude_shared_surfaces import _turn as _shared_turn
from test_claude_shared_surfaces import _update as _shared_update
from test_session_directive_input_digest import (  # noqa: F401  (_clean_queue: fixture)
    CALL_ARGS,
    _assert_record_applied_once,
    _clean_queue,
    _drive,
    _stub_state,
)

from kiro_crew import session_directive
from kiro_crew.acp.types import (
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_TOOL_RESULT,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.dashboard import directive_queue
from kiro_crew.dashboard.chat_utils import effective_session_key

CAPTURE = Path(__file__).parent / "fixtures" / "claude_native_subagents" / "agent-task-0.94.0.jsonl"
PARENT = "parent-0001"
CHILD = "child-0001"
AGENT_CALL = "toolu_0001"
CALL = "toolu_late"
TOOL = "mcp__kirocrew-core__monitor_start"


def _linked_child() -> list[JsonRpcMessage]:
    """The capture's frames up to the point the child and its Agent call are linked."""
    lines = [json.loads(line) for line in CAPTURE.read_text(encoding="utf-8").splitlines()]
    frames = [line for line in lines[1:] if "_turn_end" not in line]
    return [JsonRpcMessage.from_dict(frame) for frame in frames[:7]]


def _eager_call_update() -> dict[str, Any]:
    """The call a permission request emitted on the asking session, naming no parent."""
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": CALL,
        "title": TOOL,
        "kind": "other",
        "status": "pending",
        "rawInput": CALL_ARGS,
        "_meta": {"claudeCode": {"toolName": TOOL}},
    }


def _refinement_update(stamp: str | None, *, final: bool) -> dict[str, Any]:
    """The streamed update that first names the call's parent, or a later one."""
    meta: dict[str, Any] = {"toolName": TOOL}
    if stamp:
        meta["parentToolUseId"] = stamp
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": CALL,
        "_meta": {"claudeCode": meta},
    }
    if final:
        marker = session_directive.encode("monitor_start", CALL_ARGS, "armed")
        update["status"] = "completed"
        update["content"] = [{"type": "content", "content": {"type": "text", "text": marker}}]
    return update


def _late_frames(session_id: str, stamp: str | None, final: bool) -> list[JsonRpcMessage]:
    """The eager call on the parent's session, then the update that names its parent.

    *final*: that update also carries the call's result. Otherwise a later final
    update carries it, as a long call's progress updates come first.
    """
    frames = [_update(PARENT, _eager_call_update())]
    frames.append(_update(session_id, _refinement_update(stamp, final=final)))
    if not final:
        frames.append(_update(session_id, _refinement_update(stamp, final=True)))
    return frames


def _marks(events: list[AcpEvent]) -> list[tuple[str, str]]:
    return [
        (str(e.sub_session_id), str(e.tool_call_id))
        for e in events
        if e.kind == EVENT_SUBAGENT_ACTIVITY and e.tool_call_id
    ]


def _assert_marked_before_result(
    events: list[AcpEvent], child_key: str, *, result: bool = True
) -> None:
    """*CALL* is marked as *child_key*'s only, and first ahead of its result.

    A call may be marked again on each later update (every reader of the mark is
    idempotent), so the check is on which child the marks name and where the first
    one lands, never on how many there are.
    """
    assert set(_marks(events)) == {(child_key, CALL)}
    first_mark = next(
        i
        for i, e in enumerate(events)
        if e.kind == EVENT_SUBAGENT_ACTIVITY and e.tool_call_id == CALL
    )
    results = [
        i for i, e in enumerate(events) if e.kind == EVENT_TOOL_RESULT and e.tool_call_id == CALL
    ]
    assert bool(results) or not result
    assert all(first_mark < i for i in results)


_FINAL = pytest.mark.parametrize("final", [False, True], ids=["refinement", "final"])

# ── The per-chat client ─────────────────────────────────────────────────────────

_CLIENT_CASES = {
    # The Agent call the update is stamped with is linked to a known child.
    "stamped": (True, PARENT, AGENT_CALL, CHILD),
    # The adapter routed the update to the child's own session once it knew it.
    "routed": (True, CHILD, None, CHILD),
    # The stamp names an Agent call this session has no link for (past the cap).
    "unlinked": (False, PARENT, "toolu_unlinked", "toolu_unlinked"),
}


@_FINAL
@pytest.mark.parametrize("case", list(_CLIENT_CASES))
@pytest.mark.asyncio
async def test_a_call_named_a_childs_only_on_an_update_is_marked_before_its_result(
    case, final
) -> None:
    linked, session_id, stamp, child_key = _CLIENT_CASES[case]
    frames = [*(_linked_child() if linked else []), *_late_frames(session_id, stamp, final)]
    events = await _turn(_client(), frames)
    # Marked by the first update that names it, ahead of the call's result.
    _assert_marked_before_result(events, child_key)


@pytest.mark.asyncio
async def test_a_call_named_a_childs_on_its_first_frame_is_marked_there() -> None:
    stamped = {**_eager_call_update(), "_meta": {"claudeCode": {"parentToolUseId": AGENT_CALL}}}
    frames = [
        *_linked_child(),
        _update(PARENT, stamped),
        _update(PARENT, _refinement_update(AGENT_CALL, final=True)),
    ]
    _assert_marked_before_result(await _turn(_client(), frames), CHILD)


@pytest.mark.asyncio
async def test_the_parents_own_call_refined_by_an_update_is_never_marked() -> None:
    """An update that names no parent and stays on the parent's session is the parent's."""
    events = await _turn(_client(), _late_frames(PARENT, None, True))
    assert not _marks(events)


# ── The dashboard turn ──────────────────────────────────────────────────────────


@_FINAL
@pytest.mark.asyncio
async def test_a_late_named_childs_directive_does_not_act_on_the_parents_chat(
    tmp_path, monkeypatch, final
) -> None:
    """The child's directive is refused, and the record its tool parked under the
    parent's session is retired so no later frame can claim it."""
    events = await _turn(_client(), [*_linked_child(), *_late_frames(PARENT, AGENT_CALL, final)])
    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("late-child")
    slot._titled = True
    spy = await _drive(state, slot, events, monkeypatch)
    spy.assert_not_called()
    assert directive_queue.depth(effective_session_key(slot)) == 0


@pytest.mark.asyncio
async def test_the_parents_own_late_refined_directive_still_applies(tmp_path, monkeypatch) -> None:
    """The same frames with no parent named are the parent's call: it applies once."""
    events = await _turn(_client(), _late_frames(PARENT, None, True))
    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("late-parent")
    slot._titled = True
    spy = await _drive(state, slot, events, monkeypatch)
    _assert_record_applied_once(spy, slot)


# ── The shared runtime's session handle ─────────────────────────────────────────

SHARED_CHILD = "child-0001"
SHARED_AGENT_CALL = "toolu_agent"


def _shared_announce() -> dict[str, Any]:
    return _shared_update(
        "s-a",
        {
            "sessionUpdate": "subagent_update",
            "sessionId": SHARED_CHILD,
            "title": "general-purpose",
            "description": "Count the files",
            "state": {"state": "running"},
        },
    )


_SHARED_PROMPT = _shared_update(
    SHARED_CHILD,
    {
        "sessionUpdate": "session_message",
        "messageId": SHARED_AGENT_CALL,
        "content": [{"type": "text", "text": "Count the files."}],
    },
)

_SHARED_CASES = {
    "stamped": ("s-a", SHARED_AGENT_CALL, SHARED_CHILD),
    "routed": (SHARED_CHILD, None, SHARED_CHILD),
    "unlinked": ("s-a", "toolu_unknown_agent", "toolu_unknown_agent"),
}


@_FINAL
@pytest.mark.parametrize("case", list(_SHARED_CASES))
@pytest.mark.usefixtures("harness")
def test_the_shared_handle_marks_a_call_named_a_childs_only_on_an_update(
    tmp_path, case, final
) -> None:
    session_id, stamp, child_key = _SHARED_CASES[case]
    frames = [_shared_update("s-a", _eager_call_update())]
    frames.append(_shared_update(session_id, _refinement_update(stamp, final=final)))
    if not final:
        frames.append(_shared_update(session_id, _refinement_update(stamp, final=True)))

    async def scenario(rt, chat_a, chat_b, stdout):
        return await _shared_turn(
            rt, chat_a, stdout, [_shared_announce(), _SHARED_PROMPT, *frames, *_answer()]
        )

    # A frame routed to the child's own session reaches the parent as activity only.
    _assert_marked_before_result(
        _run(tmp_path, scenario), child_key, result=session_id != SHARED_CHILD
    )
