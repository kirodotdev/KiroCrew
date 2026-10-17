"""A drained completion that Shared Crew admission never runs still settles its owner.

Every hand-off of a sub-agent completion to its parent tab (the idle tab's
``queue_append`` and immediate drain, the busy tab's ``queue_insert`` drained at
the busy turn's end, and the stall kick) pops the row in
``_start_next_queued_turn`` and starts the guarded ``_run_chat``. With fair
admission on, ``gate_turn`` can return without running the turn body: the
admission is rejected, or its wait ends before the turn runs. A cancel can also
end the turn while it waits, and the turn can fail to start at all. The turn body
then never registered the completion's settlement, so the drain's own pre-entry
owner settles it. A person's Stop or the tab's teardown discards it, and the agent's
later goal stop applies. Any other end puts the row back in the queue under the
completion retry hold: the result is still owed, so the goal stop stays refused
until a later turn delivers it to the model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_runner_coverage_helpers import _complete, _runner_state, _slot
from test_autonudge_stop_auth import _FakeLoop, _FakeSvc, _install_svc

from kiro_crew import session_directive
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.chat_utils import SUBAGENT_COMPLETION_KIND
from kiro_crew.providers.base import EVENT_TEXT_CHUNK, EVENT_TOOL_CALL, EVENT_TOOL_RESULT, LLMEvent
from kiro_crew.shared_crew import admission_gate, turn_gate
from kiro_crew.shared_crew.admission import ReasonCode
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_persistence import create_agent_folder, list_orphans
from kiro_crew.testing.wait import await_until, until_parked

COMPLETION = "[Subagent completion event] the child finished"
STOP_REQUEST = "Every child has reported; end the goal."


class _EndedAdmission(admission_gate.Admission):
    """A queued admission whose wait ends before the turn runs."""

    #: What ends the wait besides its expiry: a person's Stop, or the tab closing.
    on_end: Callable[[], None] | None = None

    async def run(self, coro, *, revalidate=None):
        coro.close()
        if self.on_end is not None:
            self.on_end()
        raise admission_gate.AdmissionEnded(self.work)


class _ParkedAdmission(admission_gate.Admission):
    """A queued admission still waiting for its turn when something cancels it."""

    def __init__(self) -> None:
        work = SimpleNamespace(work_id="w-parked", state=SimpleNamespace(value="queued"))
        super().__init__(service=MagicMock(), decision="queued", work=work)
        self.waiting = asyncio.Event()

    async def run(self, coro, *, revalidate=None):
        self.waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            coro.close()


def _admission(outcome: str, slot=None) -> admission_gate.Admission:
    if outcome == "rejected":
        return admission_gate.Admission(
            service=None, decision="rejected", reason=ReasonCode.CREW_ACTIVE_LIMIT
        )
    reason = ReasonCode.QUEUE_EXPIRED if outcome == "wait expired" else ReasonCode.CANCELLED_BY_USER
    work = SimpleNamespace(work_id="w-ended", state=SimpleNamespace(value="ended"), reason=reason)
    admission = _EndedAdmission(service=MagicMock(), decision="queued", work=work)
    if outcome == "stop pressed":
        admission.on_end = lambda: setattr(slot, "_stopping", True)
    elif outcome == "slot closed":
        admission.on_end = lambda: setattr(slot, "_closing", 1)
    return admission


def _force_admission(monkeypatch, admission: admission_gate.Admission | None) -> AsyncMock:
    """Make the real ``gate_turn`` see *admission* (None: fair admission off)."""
    admit = AsyncMock(return_value=admission)
    monkeypatch.setattr(turn_gate, "_start_parked", AsyncMock(return_value=None))
    monkeypatch.setattr(turn_gate, "admit_turn", admit)
    return admit


async def _settle_background(state) -> None:
    leftover = [task for task in state._background_tasks if not task.done()]
    if leftover:
        await await_until(
            asyncio.gather(*leftover, return_exceptions=True), what="the turn's leftovers"
        )


async def _hand_completion_to_tab(
    state, slot, manager, monkeypatch, handoff: str, *, cancel_when: asyncio.Event | None = None
) -> None:
    """Route one finished child's completion to *slot* through *handoff*.

    With *cancel_when*, the completion's turn is cancelled once that event is set,
    with no Stop behind the cancel.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    parent_key = f"dashboard:{slot.key}"
    info = SubagentInfo(id="child-1", task="t", parent_session_key=parent_key)
    info.done = True
    info.result = "the child's report"
    create_agent_folder(info.id, task="t", parent_session=parent_key)
    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway.dashboard_state = state
    gateway.subagent_mgr = manager
    busy_turn = None
    if handoff != "idle tab":
        # A turn that outlasts the injection wait: the completion queues behind it.
        monkeypatch.setattr("kiro_crew.slack.gateway.INJECTION_TIMEOUT", 0.01)
        busy_turn = slot.task = asyncio.get_running_loop().create_future()
    await GatewayOrchestrator._route_subagent_to_tab(
        gateway,
        info,
        announce=COMPLETION,
        body="the child's report",
        title="child-1 finished",
        sub_meta={},
        carried=(),
        _flush_only=False,
        _injection_slot=slot,
        _injection_slot_name=slot.key,
        parent_key=parent_key,
    )
    if busy_turn is not None:
        assert [item.get("kind") for item in slot._queue] == [SUBAGENT_COMPLETION_KIND]
        assert manager.terminal_delivery_inflight_for(parent_key) is True
        busy_turn.cancel()
        slot.task = None
        if handoff == "busy tab":
            # The busy turn's end hands its queue off.
            await chat_runner._start_next_queued_turn(state, slot)
        else:
            await chat_runner.drain_idle_parent_queue_for_stall(state, parent_key)
    if slot.task is not None and cancel_when is not None:
        await await_until(cancel_when, what="the completion's turn to wait")
        slot.task.cancel()
        await await_until(
            asyncio.gather(slot.task, return_exceptions=True), what="the cancelled turn"
        )
    elif slot.task is not None:
        await await_until(slot.task, what="the completion's turn")
    await _settle_background(state)


def _stop_turn_stream(operation: str, prompts: list[str]):
    stop_call = LLMEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id="tc-stop",
        title=operation,
        tool_name=operation,
        mcp_server_name=session_directive.CORE_MCP_SERVER,
    )
    stop_result = LLMEvent(
        kind=EVENT_TOOL_RESULT,
        tool_call_id="tc-stop",
        tool_output=session_directive.encode(
            operation, {"reason": "the child reported"}, "Stop requested."
        ),
        tool_final=True,
    )

    async def _stream(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        yield stop_call
        yield stop_result
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="The goal is met.")
        yield _complete()

    return _stream


def _stop_outcomes(state) -> list[str]:
    return [
        call.args[1]["output"]
        for call in state.broadcast_ws.call_args_list
        if call.args[:1] == ("tool_result",) and call.args[1].get("tool_call_id") == "tc-stop"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["autonudge_stop", "monitor_stop"])
@pytest.mark.parametrize("handoff", ["idle tab", "busy tab", "stall kick"])
@pytest.mark.parametrize("outcome", ["stop pressed", "slot closed"])
async def test_a_completion_admission_never_runs_is_discarded_and_the_goal_stop_applies(
    tmp_path, monkeypatch, outcome, handoff, operation
):
    """A Stop or the tab's teardown ends admission before the turn body: it settles once.

    The row is not left queued or re-queued, the manager drops it, and the
    agent's own goal stop in the next turn is applied.
    """
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path / "agents")
    state, client = _runner_state(tmp_path)
    slot = _slot("chat-admission-refused")
    state._slots[slot.key] = slot
    parent_key = f"dashboard:{slot.key}"
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    state.subagents = manager
    svc = _FakeSvc(_FakeLoop("loop-admission"))
    _install_svc(monkeypatch, svc)
    prompts: list[str] = []
    client.stream = MagicMock(side_effect=_stop_turn_stream(operation, prompts))
    admit = _force_admission(monkeypatch, _admission(outcome, slot))

    await _hand_completion_to_tab(state, slot, manager, monkeypatch, handoff)
    # The Stop has finished, or the session's tab is open again.
    slot._stopping = False
    slot._closing = 0

    admit.assert_awaited()
    assert prompts == [], "the refused completion turn reached the model"
    assert not any(COMPLETION in item.get("content", "") for item in slot._queue)
    assert manager.terminal_delivery_inflight_for(parent_key) is False

    # Admission lets the next turn run; the agent ends its goal there.
    _force_admission(monkeypatch, None)
    await _run_chat(state, slot, STOP_REQUEST)
    await _settle_background(state)

    outcomes = _stop_outcomes(state)
    assert len(outcomes) == 1 and "stopped on this session" in outcomes[0], outcomes
    assert svc.removed == ["loop-admission"]


def _answer_or_stop(operation: str, prompts: list[str]):
    """The agent stops its goal when asked to; any other prompt it just answers."""
    stop_stream = _stop_turn_stream(operation, prompts)

    async def _stream(prompt, *args, **kwargs):
        # A turn after a session reset carries the tab's history ahead of its text.
        if prompt.endswith(STOP_REQUEST):
            async for event in stop_stream(prompt, *args, **kwargs):
                yield event
            return
        prompts.append(prompt)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Noted.")
        yield _complete()

    return _stream


async def _turn_and_its_hand_off(state, slot, message: str) -> None:
    """Run one turn; its end may start the held completion, which runs too."""
    await _run_chat(state, slot, message)
    if slot.task is not None:
        await await_until(slot.task, what="the turn its hand-off started")
    await _settle_background(state)


def _orphan_ids() -> list[str]:
    """The children a restart would announce again: no ``delivered`` tombstone yet."""
    return [orphan["id"] for orphan in list_orphans()]


async def _requeued_until_the_next_turn_delivers_it(
    state, slot, manager, monkeypatch, prompts: list[str]
) -> None:
    """The completion waits in the queue, retry held, until a later turn delivers it.

    Nothing is tombstoned while it waits, so a restart before then announces the
    child again. The next turn that runs releases the hold, and its hand-off gives
    the model the result.
    """
    parent_key = f"dashboard:{slot.key}"
    assert prompts == [], "the unstarted completion turn reached the model"
    rows = [item for item in slot._queue if item.get("kind") == SUBAGENT_COMPLETION_KIND]
    assert [row["content"] for row in rows] == [COMPLETION]
    assert slot._held_completion_retries == {rows[0]["id"]}
    assert manager.terminal_delivery_inflight_for(parent_key) is True
    assert "child-1" in _orphan_ids()

    _force_admission(monkeypatch, None)
    await _turn_and_its_hand_off(state, slot, "Anything new from the child?")

    assert prompts[0] == "Anything new from the child?"
    assert any(COMPLETION in prompt for prompt in prompts[1:]), prompts
    assert not any(item.get("kind") == SUBAGENT_COMPLETION_KIND for item in slot._queue)
    assert manager.terminal_delivery_inflight_for(parent_key) is False
    assert "child-1" not in _orphan_ids()


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff", ["idle tab", "busy tab", "stall kick"])
@pytest.mark.parametrize("outcome", ["rejected", "wait expired"])
async def test_a_refused_completion_is_requeued_and_the_next_admitted_turn_delivers_it(
    tmp_path, monkeypatch, outcome, handoff
):
    """Refused, or its wait ended, before the turn body: the result is not lost."""
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path / "agents")
    state, client = _runner_state(tmp_path)
    slot = _slot("chat-admission-requeued")
    state._slots[slot.key] = slot
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    state.subagents = manager
    prompts: list[str] = []
    client.stream = MagicMock(side_effect=_answer_or_stop("autonudge_stop", prompts))
    _force_admission(monkeypatch, _admission(outcome))

    await _hand_completion_to_tab(state, slot, manager, monkeypatch, handoff)

    await _requeued_until_the_next_turn_delivers_it(state, slot, manager, monkeypatch, prompts)


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff", ["idle tab", "busy tab", "stall kick"])
async def test_a_bare_cancel_while_the_turn_waits_requeues_the_completion(
    tmp_path, monkeypatch, handoff
):
    """A cancel with no Stop behind it ends the turn before its body: nothing is lost.

    No one pressed Stop and the tab is still open, so only the turn ended: the row
    goes back in the queue retry held, and the next turn that runs delivers it.
    """
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path / "agents")
    state, client = _runner_state(tmp_path)
    slot = _slot("chat-admission-cancelled")
    state._slots[slot.key] = slot
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    state.subagents = manager
    prompts: list[str] = []
    client.stream = MagicMock(side_effect=_answer_or_stop("autonudge_stop", prompts))
    parked = _ParkedAdmission()
    _force_admission(monkeypatch, parked)

    await _hand_completion_to_tab(
        state, slot, manager, monkeypatch, handoff, cancel_when=parked.waiting
    )

    assert not slot._stopping
    await _requeued_until_the_next_turn_delivers_it(state, slot, manager, monkeypatch, prompts)


@pytest.mark.asyncio
async def test_a_completion_turn_that_fails_to_start_is_requeued(tmp_path, monkeypatch):
    """The drain cannot start the completion's turn: the row is requeued, not discarded.

    The start raises before the turn body exists, so the drain's pre-entry owner
    settles the row as for a refusal, and the next turn that runs delivers it.
    """
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path / "agents")
    state, client = _runner_state(tmp_path)
    slot = _slot("chat-turn-start-failed")
    state._slots[slot.key] = slot
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    state.subagents = manager
    prompts: list[str] = []
    client.stream = MagicMock(side_effect=_answer_or_stop("autonudge_stop", prompts))
    _force_admission(monkeypatch, None)
    spawn = chat_runner.spawn_guarded_turn

    def _failed_start(_state, _slot, coro):
        coro.close()
        raise RuntimeError("the turn could not start")

    monkeypatch.setattr(chat_runner, "spawn_guarded_turn", _failed_start)
    with pytest.raises(RuntimeError, match="could not start"):
        # The busy turn's end drains the completion into the failing start.
        await _hand_completion_to_tab(state, slot, manager, monkeypatch, "busy tab")
    await _settle_background(state)
    monkeypatch.setattr(chat_runner, "spawn_guarded_turn", spawn)

    await _requeued_until_the_next_turn_delivers_it(state, slot, manager, monkeypatch, prompts)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["autonudge_stop", "monitor_stop"])
async def test_the_goal_stop_waits_for_a_requeued_completion_and_applies_once_it_is_delivered(
    tmp_path, monkeypatch, operation
):
    """A result admission refused keeps the goal-stop fence until the model reads it.

    The agent's stop in the turn that runs first is refused while the result waits
    in the queue. That turn's end delivers it, and the agent's next stop applies.
    """
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path / "agents")
    state, client = _runner_state(tmp_path)
    slot = _slot("chat-admission-fence")
    state._slots[slot.key] = slot
    parent_key = f"dashboard:{slot.key}"
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    state.subagents = manager
    svc = _FakeSvc(_FakeLoop("loop-admission-fence"))
    _install_svc(monkeypatch, svc)
    prompts: list[str] = []
    client.stream = MagicMock(side_effect=_answer_or_stop(operation, prompts))
    _force_admission(monkeypatch, _admission("rejected"))
    await _hand_completion_to_tab(state, slot, manager, monkeypatch, "idle tab")
    _force_admission(monkeypatch, None)

    await _turn_and_its_hand_off(state, slot, STOP_REQUEST)

    refused = _stop_outcomes(state)
    assert len(refused) == 1 and "terminal child report delivery" in refused[0], refused
    assert svc.removed == []
    assert any(COMPLETION in prompt for prompt in prompts), prompts
    assert manager.terminal_delivery_inflight_for(parent_key) is False

    await _turn_and_its_hand_off(state, slot, STOP_REQUEST)

    outcomes = _stop_outcomes(state)
    assert len(outcomes) == 2 and "stopped on this session" in outcomes[1], outcomes
    assert svc.removed == ["loop-admission-fence"]


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff", ["idle tab", "busy tab"])
async def test_an_admitted_completion_that_registered_its_settlement_is_unchanged(
    tmp_path, monkeypatch, handoff
):
    """A completion whose turn body ran keeps the turn as its one owner.

    The turn fails before the model reads it, so the turn's own settlement moves
    the completion onto a held retry row; the drain's pre-entry owner adds no
    discard, and the manager still retains the completion.
    """
    from kiro_crew.acp.client import AcpError
    from kiro_crew.dashboard.chat_utils import LIFECYCLE_RECOVERY_KIND

    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path / "agents")
    state, client = _runner_state(tmp_path)
    slot = _slot("chat-admission-ran")
    state._slots[slot.key] = slot
    parent_key = f"dashboard:{slot.key}"
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    state.subagents = manager
    prompts: list[str] = []

    async def _failing(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        raise AcpError("provider rejected the prompt", transient=False)
        yield  # pragma: no cover -- makes _failing an async generator

    client.stream = MagicMock(side_effect=_failing)
    _force_admission(monkeypatch, None)

    await _hand_completion_to_tab(state, slot, manager, monkeypatch, handoff)

    assert len(prompts) == 1 and COMPLETION in prompts[0]
    rows = [item for item in slot._queue if item.get("kind") == LIFECYCLE_RECOVERY_KIND]
    assert [row["content"] for row in rows] == [COMPLETION]
    assert slot._held_completion_retries == {rows[0]["id"]}
    assert manager.terminal_delivery_inflight_for(parent_key) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["autonudge_stop", "monitor_stop"])
async def test_a_route_cancelled_while_it_waits_for_the_tab_lock_retains_nothing(
    tmp_path, monkeypatch, operation
):
    """A completion cancelled before its hand-off leaves the manager owning nothing.

    The report task's deadline can cancel the route while it waits for the idle
    tab's lock, which another writer holds. Nothing was queued, so no row can
    release the completion, and the agent's own goal stop in the next turn must
    still apply.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path / "agents")
    state, client = _runner_state(tmp_path)
    slot = _slot("chat-lock-wait-cancelled")
    state._slots[slot.key] = slot
    parent_key = f"dashboard:{slot.key}"
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    state.subagents = manager
    svc = _FakeSvc(_FakeLoop("loop-lock-wait"))
    _install_svc(monkeypatch, svc)
    prompts: list[str] = []
    stop_stream = _stop_turn_stream(operation, prompts)

    async def _stream(prompt, *args, **kwargs):
        # Only the agent's own turn stops the goal; the synthesis turn the
        # completion armed before its wait just answers.
        if prompt == STOP_REQUEST:
            async for event in stop_stream(prompt, *args, **kwargs):
                yield event
            return
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Noted.")
        yield _complete()

    client.stream = MagicMock(side_effect=_stream)
    _force_admission(monkeypatch, None)
    info = SubagentInfo(id="child-1", task="t", parent_session_key=parent_key)
    info.done = True
    info.result = "the child's report"
    create_agent_folder(info.id, task="t", parent_session=parent_key)
    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway.dashboard_state = state
    gateway.subagent_mgr = manager

    async with slot._lock:
        route = asyncio.create_task(
            GatewayOrchestrator._route_subagent_to_tab(
                gateway,
                info,
                announce=COMPLETION,
                body="the child's report",
                title="child-1 finished",
                sub_meta={},
                carried=(),
                _flush_only=False,
                _injection_slot=slot,
                _injection_slot_name=slot.key,
                parent_key=parent_key,
            )
        )
        await until_parked(slot._lock)
        route.cancel()
        with pytest.raises(asyncio.CancelledError):
            await route

    assert not any(COMPLETION in item.get("content", "") for item in slot._queue)
    assert manager.terminal_delivery_inflight_for(parent_key) is False

    await _run_chat(state, slot, STOP_REQUEST)
    await _settle_background(state)

    outcomes = _stop_outcomes(state)
    assert len(outcomes) == 1 and "stopped on this session" in outcomes[0], outcomes
    assert svc.removed == ["loop-lock-wait"]
