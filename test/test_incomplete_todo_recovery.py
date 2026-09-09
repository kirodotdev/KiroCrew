"""Open TODO items at a normal ``end_turn`` keep the turn from landing.

A provider may emit a normal terminal message after successful tools while
items of the model's own TODO are still open. The runner then declines to
record success and tells the user which items are open. Notice-only: nothing
is scheduled on the model's TODO, and a turn with no TODO is not judged.
Explicit handoffs and user control win.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.types import STOP_REASON_CANCELLED, STOP_REASON_END_TURN
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.chat_utils import (
    _PROMISE_ONLY_CONTINUE_MSG,
    _SYNTHETIC_RECOVERY_MSGS,
    is_promise_only_terminal,
    should_notice_unfinished_todo,
)
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TODO_UPDATE,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    LLMEvent,
)

_END = STOP_REASON_END_TURN
_PENDING = {
    "description": "Finish validation",
    "tasks": [
        {"id": "1", "text": "Run focused tests", "completed": True},
        {"id": "2", "text": "Run package build", "completed": False},
        {"id": "3", "text": "Verify rendered UI", "completed": False},
    ],
}
_COMPLETE = {
    "description": "Finish validation",
    "tasks": [
        {"id": "1", "text": "Run focused tests", "completed": True},
        {"id": "2", "text": "Run package build", "completed": True},
    ],
}


def _notice(**overrides: object) -> bool:
    values: dict[str, object] = {
        "pending_todo_items": 2,
        "stop_reason": _END,
        "end_turn_reason": _END,
        "prompt_depth": 0,
        "is_cancelled": False,
        "refusal_reasons": [],
        "in_stage_execution": False,
        "terminal_question_posted": False,
        "handoff_pending": False,
        "subagents_attached": False,
        "stop_in_progress": False,
        "stop_generation_unchanged": True,
        "queue_empty": True,
        "no_pending_steers": True,
    }
    values.update(overrides)
    return should_notice_unfinished_todo(**values)  # type: ignore[arg-type]


# ── pure predicates ───────────────────────────────────────────────────────


def test_only_open_items_of_the_current_turn_are_evidence() -> None:
    assert _notice(pending_todo_items=1) is True
    assert _notice(pending_todo_items=0) is False


def test_user_control_and_handoffs_win() -> None:
    assert _notice(handoff_pending=True) is False
    assert _notice(subagents_attached=True) is False
    assert _notice(stop_in_progress=True) is False
    assert _notice(stop_generation_unchanged=False) is False
    assert _notice(queue_empty=False) is False
    assert _notice(no_pending_steers=False) is False
    assert _notice(terminal_question_posted=True) is False
    assert _notice(is_cancelled=True, stop_reason=STOP_REASON_CANCELLED) is False
    assert _notice(refusal_reasons=["policy"]) is False
    assert _notice(in_stage_execution=True) is False
    assert _notice(prompt_depth=1) is False
    assert _notice(stop_reason="max_tokens") is False


def test_handoff_in_effect_reads_state_not_the_call() -> None:
    slot = _ChatSlot.__new__(_ChatSlot)
    slot.key = "handoff-slot"
    slot._pending_reset_history_key = None
    slot._pending_discard_conversation_key = None
    slot._pending_synthesis = False
    with patch.object(chat_runner, "get_instance", return_value=None):
        assert chat_runner._handoff_in_effect(slot) is False
    armed = MagicMock(get_by_slot=MagicMock(return_value=MagicMock(active=True)))
    with patch.object(chat_runner, "get_instance", return_value=armed):
        assert chat_runner._handoff_in_effect(slot) is True
    stopped = MagicMock(get_by_slot=MagicMock(return_value=MagicMock(active=False)))
    with patch.object(chat_runner, "get_instance", return_value=stopped):
        assert chat_runner._handoff_in_effect(slot) is False
    slot._pending_reset_history_key = "dashboard:handoff-slot"
    with patch.object(chat_runner, "get_instance", return_value=None):
        assert chat_runner._handoff_in_effect(slot) is True
    # The last sub-agent completion arms a synthesis turn the runner dispatches
    # itself; the open items belong to that turn, not to the completion turn.
    slot._pending_reset_history_key = None
    slot._pending_synthesis = True
    with patch.object(chat_runner, "get_instance", return_value=None):
        assert chat_runner._handoff_in_effect(slot) is True


def test_no_continuation_message_exists_for_open_todo_items() -> None:
    """Notice-only: the synthetic recovery set is upstream's; nothing here
    queues a turn on the model's TODO."""
    assert not any("TODO" in msg for msg in _SYNTHETIC_RECOVERY_MSGS)


def test_packaged_prompt_states_the_todo_discipline() -> None:
    root = Path(__file__).resolve().parents[1]
    prompt = (root / "src" / "kiro_crew" / "config" / "prompt.md").read_text(encoding="utf-8")
    assert "create a native TODO list before the first substantive tool call" in prompt
    assert "replace or clear any stale TODO" in prompt
    assert "mark each obligation completed rather than deleting it" in prompt
    # Scoped to where it is true: the dashboard runner reads the TODO at end_turn.
    assert (
        "In a dashboard session, a turn that ends with TODO items still open is not "
        "recorded as complete" in prompt
    )


# ── slot state ────────────────────────────────────────────────────────────


def _bare_slot() -> _ChatSlot:
    slot = _ChatSlot.__new__(_ChatSlot)
    slot._todo = None
    slot._todo_work_generation = 0
    slot._todo_owner_generation = -1
    return slot


def test_pending_count_reads_the_current_turns_todo() -> None:
    slot = _bare_slot()
    assert slot.pending_todo_count() == 0
    slot.set_todo(_PENDING)
    assert slot.pending_todo_count() == 2
    slot.set_todo(_COMPLETE)
    assert slot.pending_todo_count() == 0
    slot.set_todo(None)
    assert slot.pending_todo_count() == 0


def test_new_user_turn_does_not_inherit_an_earlier_todo() -> None:
    slot = _bare_slot()
    slot.set_todo(_PENDING)
    assert slot.pending_todo_count() == 2

    slot.begin_todo_work_turn()
    # The list stays visible in the pill but speaks for the turn that wrote it.
    assert slot.todo_payload() is not None
    assert slot.pending_todo_count() == 0

    # Re-asserting it in the new turn makes it this turn's evidence again.
    slot.set_todo(_PENDING)
    assert slot.pending_todo_count() == 2


def test_set_todo_still_reports_whether_the_snapshot_changed() -> None:
    slot = _bare_slot()
    assert slot.set_todo(_PENDING) is True
    assert slot.set_todo(_PENDING) is False
    assert slot.set_todo(_COMPLETE) is True


# ── turn integration ──────────────────────────────────────────────────────


def _state_and_slot(tmp_path: Path):
    state = _make_state(tmp_path)
    client = MagicMock()
    client.context_usage_pct = MagicMock(return_value=50.0)
    client.shutdown = AsyncMock()
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.record_success = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    # No sub-agent registry: the attached-probe answers "none", so the tests
    # below that want sub-agents attached opt in explicitly.
    state.subagents = None
    slot = state.get_or_create_slot("incomplete-todo-slot")
    slot.append("user", "finish the implementation", "msg msg-u")
    return state, slot, client


async def _cancel_background_tasks(state) -> None:
    tasks = list(state._background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _tool(index: int) -> list[LLMEvent]:
    tool_id = f"tc-{index}"
    return [
        LLMEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id=tool_id,
            title=f"step {index}",
            tool_name=f"validation_{index}",
            tool_kind="execute",
        ),
        LLMEvent(kind=EVENT_TOOL_RESULT, tool_call_id=tool_id, text="passed"),
    ]


def _install_stream(
    client, todo: dict | None, final_text: str, executed: list[str], *, tool_count: int = 1
) -> None:
    tools = [ev for i in range(1, tool_count + 1) for ev in _tool(i)]

    async def stream(message: str):
        if todo is not None:
            yield LLMEvent(kind=EVENT_TODO_UPDATE, todo=todo)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Validation is in progress. ")
        executed.append(message)
        for ev in tools:
            yield ev
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=final_text)
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

    client.stream = stream
    client.stream_command = stream


class _QueueSpy:
    """Record every queue_insert so tests can assert nothing was scheduled."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._original = _ChatSlot.queue_insert

    def __enter__(self) -> "_QueueSpy":
        original = self._original
        calls = self.calls

        def spy(self_slot, *args, **kwargs):
            calls.append(args)
            return original(self_slot, *args, **kwargs)

        self._patch = patch.object(_ChatSlot, "queue_insert", spy)
        self._patch.start()
        return self

    def __exit__(self, *exc) -> None:
        self._patch.stop()


def _notices(slot) -> list[str]:
    return [str(m.get("content", "")) for m in slot.messages if m.get("role") == "notice"]


@pytest.mark.asyncio
async def test_open_todo_items_keep_the_turn_unlanded_and_schedule_nothing(
    tmp_path: Path,
) -> None:
    """Exact field shape: tools succeed, the provider says it is continuing, but
    two of the model's own TODO items are open. Not a success; nothing queued."""
    state, slot, client = _state_and_slot(tmp_path)
    executed: list[str] = []
    _install_stream(client, _PENDING, "Understood. Continuing without pausing.", executed)

    with _QueueSpy() as spy:
        await _run_chat(state, slot, "finish the implementation")
        await _cancel_background_tasks(state)

    assert spy.calls == []
    assert executed == ["finish the implementation"]
    assert any("2 TODO item(s) still open" in n for n in _notices(slot))
    state.sessions.record_success.assert_not_called()


@pytest.mark.asyncio
async def test_a_completed_todo_lands_normally(tmp_path: Path) -> None:
    state, slot, client = _state_and_slot(tmp_path)
    executed: list[str] = []
    _install_stream(client, _COMPLETE, "Both checks pass.", executed, tool_count=2)

    with _QueueSpy() as spy:
        await _run_chat(state, slot, "run lint and the unit tests")
        await _cancel_background_tasks(state)

    assert spy.calls == []
    assert not any("still open" in n for n in _notices(slot))
    state.sessions.record_success.assert_called_once()


@pytest.mark.asyncio
async def test_a_turn_without_a_todo_is_not_judged(tmp_path: Path) -> None:
    """No TODO, no evidence: the runtime does not guess from tool counts or prose."""
    state, slot, client = _state_and_slot(tmp_path)
    executed: list[str] = []
    _install_stream(client, None, "Yes.", executed, tool_count=3)

    with _QueueSpy() as spy:
        await _run_chat(state, slot, "finish the implementation")
        await _cancel_background_tasks(state)

    assert spy.calls == []
    assert not any("still open" in n for n in _notices(slot))
    state.sessions.record_success.assert_called_once()


@pytest.mark.asyncio
async def test_an_earlier_turns_todo_does_not_flag_a_new_request(tmp_path: Path) -> None:
    state, slot, client = _state_and_slot(tmp_path)
    executed: list[str] = []
    _install_stream(client, _PENDING, "Understood. Continuing without pausing.", executed)
    await _run_chat(state, slot, "finish the implementation")
    await _cancel_background_tasks(state)
    state.sessions.record_success.assert_not_called()

    # A new request that never touches the TODO: the old list is still shown
    # but is not this turn's evidence.
    async def plain(message: str):
        executed.append(message)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="It is 4pm in Berlin.")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

    client.stream = plain
    client.stream_command = plain
    state.sessions.record_success.reset_mock()
    await _run_chat(state, slot, "what time is it in Berlin?")
    await _cancel_background_tasks(state)

    assert slot.todo_payload() is not None
    state.sessions.record_success.assert_called_once()


@pytest.mark.asyncio
async def test_armed_loop_suppresses_the_notice(tmp_path: Path) -> None:
    """monitor_start ended the turn on purpose; the loop drives the next step."""
    state, slot, client = _state_and_slot(tmp_path)
    executed: list[str] = []
    _install_stream(client, _PENDING, "Monitoring is armed; I'll check back.", executed)
    armed = MagicMock(get_by_slot=MagicMock(return_value=MagicMock(active=True)))

    with patch.object(chat_runner, "get_instance", return_value=armed):
        await _run_chat(state, slot, "babysit the PR")
        await _cancel_background_tasks(state)

    assert not any("still open" in n for n in _notices(slot))
    state.sessions.record_success.assert_called_once()


@pytest.mark.asyncio
async def test_attached_subagents_suppress_the_notice(tmp_path: Path) -> None:
    state, slot, client = _state_and_slot(tmp_path)
    executed: list[str] = []
    _install_stream(client, _PENDING, "Spawned two agents, waiting.", executed)
    state.subagents = MagicMock(
        running_agents_for=MagicMock(return_value=["agent-1"]),
        _queued_depth=MagicMock(return_value=0),
    )

    await _run_chat(state, slot, "research these three options")
    await _cancel_background_tasks(state)

    assert not any("still open" in n for n in _notices(slot))
    state.sessions.record_success.assert_called_once()


@pytest.mark.asyncio
async def test_armed_synthesis_suppresses_the_notice(tmp_path: Path) -> None:
    """The last sub-agent completion turn updates the TODO while the runner has
    already armed the synthesis turn that finishes the items. The completion
    turn is not unfinished work: it lands, and the synthesis owns the items."""
    state, slot, client = _state_and_slot(tmp_path)
    executed: list[str] = []
    _install_stream(client, _PENDING, "Both agents reported; synthesizing next.", executed)
    slot._pending_synthesis = True

    await _run_chat(state, slot, "[Subagent completion event] agent-2 finished")
    await _cancel_background_tasks(state)

    assert not any("still open" in n for n in _notices(slot))
    state.sessions.record_success.assert_called_once()


@pytest.mark.asyncio
async def test_an_empty_final_turn_keeps_the_empty_response_ladder(tmp_path: Path) -> None:
    """Tools ran, the TODO has open items, but the model produced no final text.
    That turn belongs to the pre-existing empty-response ladder, whose recovery
    fires on the emptiness: this arm yields to it rather than shadowing it."""
    state, slot, client = _state_and_slot(tmp_path)
    tools = _tool(1) + _tool(2)

    async def stream(message: str):
        yield LLMEvent(kind=EVENT_TODO_UPDATE, todo=_PENDING)
        for ev in tools:
            yield ev
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

    client.stream = stream
    client.stream_command = stream
    with (
        _QueueSpy() as spy,
        patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await _run_chat(state, slot, "finish the implementation")
        await _cancel_background_tasks(state)

    assert not any("still open" in n for n in _notices(slot))
    ladder = [c for c in spy.calls if c and c[1] in _SYNTHETIC_RECOVERY_MSGS]
    assert len(ladder) == 1


@pytest.mark.asyncio
async def test_a_multitool_turn_without_a_todo_logs_the_blind_spot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The un-judged shape is counted, not hidden: one WARNING with closed fields."""
    state, slot, client = _state_and_slot(tmp_path)
    executed: list[str] = []
    _install_stream(client, None, "Yes.", executed, tool_count=3)

    with caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"):
        await _run_chat(state, slot, "build and install the package")
        await _cancel_background_tasks(state)

    hits = [r for r in caplog.records if "completion not judged" in r.getMessage()]
    assert len(hits) == 1
    assert "3 tool call(s)" in hits[0].getMessage()
    assert "build and install" not in hits[0].getMessage()
    assert "Yes." not in hits[0].getMessage()
    state.sessions.record_success.assert_called_once()


@pytest.mark.asyncio
async def test_a_refused_handoff_does_not_suppress_the_notice(tmp_path: Path) -> None:
    """A spawn_run that errors attaches no sub-agents, so the open items are
    still this turn's: the handoff is confirmed from state, not from the call."""
    state, slot, client = _state_and_slot(tmp_path)
    executed: list[str] = []
    _install_stream(client, _PENDING, "Spawned the reviewer, waiting.", executed)

    await _run_chat(state, slot, "research these three options")
    await _cancel_background_tasks(state)

    assert any("2 TODO item(s) still open" in n for n in _notices(slot))
    state.sessions.record_success.assert_not_called()


@pytest.mark.asyncio
async def test_promise_only_recovery_is_unchanged_by_the_notice(tmp_path: Path) -> None:
    """A zero-tool promise-only ending with open items: the promise-only arm keeps
    its own, pre-existing one-shot continuation; this arm adds only the notice."""
    state, slot, client = _state_and_slot(tmp_path)
    final = "I'll run the build now."
    assert is_promise_only_terminal(final)

    async def stream(message: str):
        yield LLMEvent(kind=EVENT_TODO_UPDATE, todo=_PENDING)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=final)
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

    client.stream = stream
    client.stream_command = stream
    with (
        _QueueSpy() as spy,
        patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await _run_chat(state, slot, "build it")
        await _cancel_background_tasks(state)

    queued = [c for c in spy.calls if c and c[1] == _PROMISE_ONLY_CONTINUE_MSG]
    assert len(queued) == 1
    assert any("2 TODO item(s) still open" in n for n in _notices(slot))
    state.sessions.record_success.assert_not_called()
