"""A cancel racing the first Go must win, and repeat cancels must be idempotent.

Revoking a plan only through the tracker (``tracker.stop()`` when
``slot._orch_tracker`` exists) is not enough: the tracker is created lazily
INSIDE ``_stage_loop``, so a Cancel processed in the sub-tick window between a Go
POST being accepted and its ``_stage_loop`` coroutine running finds no tracker,
no-ops, and appends '🛑 Plan cancelled.' — while the Go builds a fresh
(unstopped) tracker and advances stage 1. The transcript says cancelled but the
plan proceeds.

The fix is a slot-level latch, ``slot._plan_cancelled``: set unconditionally by
the Cancel handler, checked by ``_stage_loop`` before it creates a tracker, and
cleared only when a NEW plan is armed (``_reset_auto_run_for_new_plan``) — never
on Go, so a Go cannot resurrect a cancelled plan. The same handler pass also
made repeat Cancels idempotent in the transcript: the cancelled row is appended
once, not once per POST.

These tests drive the real HTTP cancel handler against a real ``_stage_loop``,
mirroring test_orchestrator_cancel_stops_advance.py.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    """Stage results are captured under ``config_dir()`` -- keep them per-test.

    ``chat_orchestrator`` imports ``config_dir`` into its own namespace, so
    patching only ``state`` would leave results writing to the live data home.
    """
    for module in ("state", "chat", "chat_orchestrator"):
        monkeypatch.setattr(f"kiro_crew.dashboard.{module}.config_dir", lambda: tmp_path)


def _make_orchestrator_state(tmp_path, slot_key, titles):
    """A state whose subagent manager reports nothing pending.

    The loop is fail-closed on the subagent check: a missing manager, or a
    ``running_agents_for`` returning None, breaks out of the loop on its own, so
    a test that left either unset would pass without the cancel doing anything.
    """
    state = _make_state(tmp_path)
    state.subagents = MagicMock()
    state.subagents.running_agents_for = MagicMock(return_value=[])
    state.subagents._tasks = {}
    slot = state.get_or_create_slot(slot_key, mode="orchestrator")
    slot._stage_titles = list(titles)
    slot._plan_goal = "Test goal"
    slot._auto_run = True
    return state, slot


async def _cancel(client, slot_key):
    resp = await client.post(f"/api/chat/slots/{slot_key}/plan-action", json={"action": "cancel"})
    assert resp.status == 200
    assert (await resp.json())["cancelled"] is True


def _cancelled_rows(slot) -> int:
    return sum(1 for m in slot.messages if "Plan cancelled" in (m.get("content") or ""))


def _arm_owned_stage_delivery(slot) -> None:
    from kiro_crew.dashboard.chat_utils import SUBAGENT_COMPLETION_KIND

    slot.stage_boundary.arm(1, consumed=True)
    announce = "[Subagent completion event]\nstage-owned result"
    slot.queue_append(
        announce,
        kind=SUBAGENT_COMPLETION_KIND,
        meta=slot.stage_boundary.tag_meta(),
    )
    slot.note_pending_subagent_delivery(announce, ["stage-agent"])


@pytest.mark.asyncio
async def test_cancel_before_stage_loop_starts_does_not_advance(tmp_path, monkeypatch):
    """Cancel processed before the stage loop creates a tracker: NO stage runs.

    This is the race window itself: the Go's ``_stage_loop`` task is created
    but has not executed its first line, so ``slot._orch_tracker`` is still
    None when the Cancel lands. The tracker guard alone no-ops here; only the
    latch can stop the pending loop.
    """
    from kiro_crew.dashboard.chat import _stage_loop

    state, slot = _make_orchestrator_state(tmp_path, "cancel-pre-loop", ["First", "Second"])

    stages_run: list[int] = []

    async def _mock_run_chat(_state, _slot, _message, **_kwargs):
        stages_run.append(len(stages_run) + 1)
        _slot.append("assistant", f"stage {len(stages_run)} body", "msg msg-a")

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        # The interleaving under test is "Cancel fully processed before _stage_loop's
        # first line runs". Awaiting the HTTP round-trip yields to the event
        # loop, which would start an already-created loop task and make the
        # ordering a coin flip — so process the cancel first, then schedule the
        # loop exactly as the Go handler does. What the loop observes is
        # identical: latch set, no tracker.
        assert slot._orch_tracker is None, "setup: the race window requires no tracker yet"
        await _cancel(client, "cancel-pre-loop")
        assert slot._plan_cancelled is True
        assert slot._orch_tracker is None, "cancel must not have created a tracker"

        loop_task = asyncio.create_task(_stage_loop(state, slot, auto_run=True))
        slot.task = loop_task
        await asyncio.wait_for(loop_task, timeout=5)

    assert stages_run == [], f"cancelled-before-start plan still advanced: ran {stages_run}"
    assert slot._orch_tracker is None, "the pending loop must not build a tracker after cancel"
    assert slot.task is None, "early exit must release the slot for later messages"


@pytest.mark.asyncio
async def test_double_cancel_appends_exactly_one_cancelled_row(tmp_path):
    """Repeat Cancel POSTs keep returning ok:true but write ONE transcript row."""
    state, slot = _make_orchestrator_state(tmp_path, "double-cancel", ["First"])

    async with TestClient(TestServer(_make_app(state))) as client:
        await _cancel(client, "double-cancel")
        await _cancel(client, "double-cancel")
        await _cancel(client, "double-cancel")

    assert slot._plan_cancelled is True
    assert (
        _cancelled_rows(slot) == 1
    ), f"expected exactly one cancelled row, transcript has {_cancelled_rows(slot)}"


@pytest.mark.asyncio
async def test_idle_cancel_settles_owned_stage_delivery_debt(tmp_path):
    """A paused boundary releases its queue row and retention debt on Cancel."""
    state, slot = _make_orchestrator_state(tmp_path, "cancel-idle-debt", ["First"])
    settled: list[list[str]] = []

    async def _settle(agent_ids: list[str]) -> None:
        settled.append(agent_ids)

    state.subagents.settle_queued_delivery = _settle
    _arm_owned_stage_delivery(slot)
    assert slot._stage_controller_task is None and not slot._in_stage_execution

    async with TestClient(TestServer(_make_app(state))) as client:
        await _cancel(client, slot.key)

    assert slot._queue == [], "idle Cancel left the stage-owned completion queued"
    assert slot._subagent_delivery_pending == {}, "idle Cancel stranded delivery debt"
    assert settled == [["stage-agent"]]
    assert slot.stage_boundary.stage is None


@pytest.mark.asyncio
async def test_typed_stop_releases_owned_stage_delivery_debt(tmp_path):
    """A typed stop settles the same owned rows and debt as plan Cancel."""
    from kiro_crew.context_management import MAX_STAGE_ROUNDS, OrchestrationTracker

    state, slot = _make_orchestrator_state(tmp_path, "typed-stop-debt", ["First"])
    tracker = OrchestrationTracker(stage_timeout_seconds=60)
    tracker._stage_rounds[1] = MAX_STAGE_ROUNDS
    slot._orch_tracker = tracker
    _arm_owned_stage_delivery(slot)
    settled: list[list[str]] = []

    async def _settle(agent_ids: list[str]) -> None:
        settled.append(agent_ids)

    state.subagents.settle_queued_delivery = _settle
    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.post("/api/chat", json={"slot": slot.key, "message": "stop"})
        assert response.status == 200 and (await response.json()).get("stopped") is True

    assert slot._queue == []
    assert slot._subagent_delivery_pending == {}
    assert settled == [["stage-agent"]]
    assert slot.stage_boundary.stage is None


@pytest.mark.asyncio
async def test_typed_stop_quiesces_controller_before_terminal_notice(tmp_path, monkeypatch):
    """A between-stage controller cannot emit after the stopped notice."""
    from kiro_crew.context_management import MAX_STAGE_ROUNDS, OrchestrationTracker

    state, slot = _make_orchestrator_state(tmp_path, "typed-stop-controller", ["First"])
    tracker = OrchestrationTracker(stage_timeout_seconds=60)
    tracker._stage_rounds[1] = MAX_STAGE_ROUNDS
    slot._orch_tracker = tracker
    slot.stage_boundary.arm(1, consumed=True)
    controller_started = asyncio.Event()
    controller_tail = "controller tail after cancellation"

    async def _controller() -> None:
        controller_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            slot.append("assistant", controller_tail, "msg msg-a")

    controller = asyncio.create_task(_controller())
    slot.track_stage_controller(controller)
    await controller_started.wait()
    # Model the typed-control admission snapshot: no child turn is active, but
    # the outer stage controller has not quiesced yet.
    monkeypatch.setattr(type(slot), "turn_running", property(lambda _slot: False))
    terminal_message = "🛑 [SYSTEM] Orchestration stopped by user."
    try:
        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post("/api/chat", json={"slot": slot.key, "message": "stop"})
            assert response.status == 200
            assert (await response.json()).get("stopped") is True
            if not controller.done():
                controller.cancel()
            await asyncio.gather(controller, return_exceptions=True)

        contents = [str(row.get("content") or "") for row in slot.messages]
        terminal_index = contents.index(terminal_message)
        assert controller_tail in contents[:terminal_index]
        assert contents[terminal_index:] == [terminal_message]
    finally:
        if not controller.done():
            controller.cancel()
            await asyncio.gather(controller, return_exceptions=True)


@pytest.mark.parametrize("surface", ["plan-action", "typed-stop"])
@pytest.mark.asyncio
async def test_plan_cancel_revokes_every_captured_parent_before_boundary_clear(tmp_path, surface):
    """Cancel revokes exact stage children before releasing ownership."""
    from kiro_crew.context_management import MAX_STAGE_ROUNDS, OrchestrationTracker

    state, slot = _make_orchestrator_state(tmp_path, f"captured-parent-{surface}", ["First"])
    slot.stage_boundary.arm(1, consumed=True)
    owner = slot.stage_boundary.owner
    assert owner
    captured_keys = {"slack:1712345678.901", "discord:channel:message"}
    slot.stage_boundary.parent_session_keys.update(captured_keys)
    dashboard_key = f"dashboard:{slot.key}"
    parent_keys = captured_keys | {dashboard_key}
    calls: list[tuple[str, str]] = []

    async def _child() -> None:
        await asyncio.Event().wait()

    children = {(key, owner): asyncio.create_task(_child()) for key in parent_keys}
    sibling_key = next(iter(sorted(parent_keys)))
    sibling = asyncio.create_task(_child())

    async def _cancel_boundary(parent_key: str, boundary_owner: str) -> tuple[int, int]:
        assert slot.stage_boundary.stage == 1, "boundary cleared before child cancellation"
        assert boundary_owner == owner
        calls.append((parent_key, boundary_owner))
        child = children.get((parent_key, boundary_owner))
        if child is not None and not child.done():
            child.cancel()
            return (1, 0)
        return (0, 0)

    state.subagents.cancel_for_boundary = _cancel_boundary
    state.subagents.cancel_for_parent = AsyncMock(
        side_effect=AssertionError("stage cancellation widened to the parent")
    )
    state.subagents.running_agents_for = MagicMock(return_value=[])
    try:
        async with TestClient(TestServer(_make_app(state))) as client:
            if surface == "plan-action":
                await _cancel(client, slot.key)
            else:
                tracker = OrchestrationTracker(stage_timeout_seconds=60)
                tracker._stage_rounds[1] = MAX_STAGE_ROUNDS
                slot._orch_tracker = tracker
                response = await client.post(
                    "/api/chat", json={"slot": slot.key, "message": "stop"}
                )
                assert response.status == 200
                assert (await response.json()).get("stopped") is True
        await asyncio.sleep(0)
        assert set(calls) == {(key, owner) for key in parent_keys}
        assert len(calls) == len(parent_keys), "a stage scope was cancelled more than once"
        assert all(child.cancelled() for child in children.values())
        assert not sibling.done(), f"sibling owner under {sibling_key} was cancelled"
        assert slot.stage_boundary.stage is None
    finally:
        for child in (*children.values(), sibling):
            if not child.done():
                child.cancel()
        await asyncio.gather(*children.values(), sibling, return_exceptions=True)


@pytest.mark.parametrize("surface", ["plan-action", "typed-stop"])
@pytest.mark.asyncio
async def test_plan_cancel_stops_controller_before_sweeping_captured_scope(
    tmp_path,
    monkeypatch,
    surface,
):
    """A child spawned while the controller unwinds is included in cancellation."""
    from kiro_crew.context_management import MAX_STAGE_ROUNDS, OrchestrationTracker

    state, slot = _make_orchestrator_state(tmp_path, f"controller-spawn-{surface}", ["First"])
    slot.stage_boundary.arm(1, consumed=True)
    owner = slot.stage_boundary.owner
    assert owner
    parent = f"dashboard:{slot.key}"
    children: list[asyncio.Task] = []
    order: list[str] = []

    def _reserve(parent_keys: tuple[str, ...], boundary_owner: str) -> str:
        assert slot.stage_boundary.stage == 1, "reservation followed controller teardown"
        assert tuple(parent_keys) == (parent,)
        assert boundary_owner == owner
        order.append("reserved")
        return ""

    state.subagents.reserve_boundary_cancellation_scopes = _reserve

    async def _child() -> None:
        await asyncio.Event().wait()

    children.append(asyncio.create_task(_child()))
    controller_started = asyncio.Event()

    async def _controller() -> None:
        controller_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            children.append(asyncio.create_task(_child()))
            slot.stage_boundary.clear()
            order.append("controller-finished")

    controller = asyncio.create_task(_controller())
    slot.track_stage_controller(controller)
    await controller_started.wait()
    monkeypatch.setattr(type(slot), "turn_running", property(lambda _slot: False))

    async def _cancel_boundary(parent_key: str, boundary_owner: str) -> tuple[int, int]:
        assert parent_key == parent
        assert boundary_owner == owner
        order.append("swept")
        stopped = 0
        for child in children:
            if not child.done():
                child.cancel()
                stopped += 1
        return (stopped, 0)

    state.subagents.cancel_for_boundary = _cancel_boundary
    try:
        async with TestClient(TestServer(_make_app(state))) as client:
            if surface == "plan-action":
                await _cancel(client, slot.key)
            else:
                tracker = OrchestrationTracker(stage_timeout_seconds=60)
                tracker._stage_rounds[1] = MAX_STAGE_ROUNDS
                slot._orch_tracker = tracker
                response = await client.post(
                    "/api/chat", json={"slot": slot.key, "message": "stop"}
                )
                assert response.status == 200
                assert (await response.json()).get("stopped") is True
        await asyncio.sleep(0)
        assert order == ["reserved", "controller-finished", "swept"]
        assert controller.done(), "the producer was not stopped before the child sweep"
        assert len(children) == 2, "the controller did not exercise the late-spawn race"
        assert all(child.cancelled() for child in children)
        assert slot.stage_boundary.stage is None
    finally:
        if not controller.done():
            controller.cancel()
        for child in children:
            if not child.done():
                child.cancel()
        await asyncio.gather(controller, *children, return_exceptions=True)


@pytest.mark.asyncio
async def test_stage_cancel_store_failure_surfaces_mirrored_halt_notice(tmp_path, monkeypatch):
    import kiro_crew.dashboard.chat_orchestrator as orchestrator

    state, slot = _make_orchestrator_state(tmp_path, "cancel-store-failure", ["First"])
    slot.stage_boundary.arm(1, consumed=True)
    owner = slot.stage_boundary.owner
    assert owner
    manager = MagicMock()
    manager.cancel_for_boundary = AsyncMock(return_value=(0, 0))
    manager.boundary_cancellation_pending_reason.return_value = "task store read failed"
    state.subagents = manager
    mirror = AsyncMock()
    monkeypatch.setattr(orchestrator, "_deliver_halt_notice_to_channels", mirror)

    scope = orchestrator._capture_stage_cancellation_scope(slot)
    await orchestrator._cancel_stage_subagents(state, slot, scope=scope)
    await asyncio.sleep(0)
    if state._background_tasks:
        await asyncio.gather(*tuple(state._background_tasks))

    notice = next(
        row["content"]
        for row in slot.messages
        if "durable task queue" in str(row.get("content", ""))
    )
    assert "remains blocked" in notice
    assert "retry" in notice
    mirror.assert_awaited_once_with(state, slot, notice)


@pytest.mark.asyncio
async def test_active_stage_cancel_scope_cap_keeps_boundary_and_blocks_dispatch(
    tmp_path, monkeypatch
):
    """A refused pre-teardown reservation keeps its stage closed."""
    import kiro_crew.dashboard.chat_orchestrator as orchestrator

    state, slot = _make_orchestrator_state(tmp_path, "cancel-scope-cap", ["First"])
    slot.stage_boundary.arm(1, consumed=True)
    reason = "pending_scope_cap: retained 2, cap 2, overflow count 1"
    order: list[str] = []
    row_dispatched = False
    manager = MagicMock()

    def _reserve(_parents: tuple[str, ...], _owner: str) -> str:
        order.append("reserved")
        if slot.stage_boundary.stage is not None:
            slot.stage_boundary.cancellation_hold_refused = reason
        return reason

    async def _cancel_boundary(
        _parent: str,
        _owner: str,
        *,
        retain_scope: bool = True,
    ) -> tuple[int, int]:
        nonlocal row_dispatched
        assert retain_scope is False
        order.append("swept")
        await asyncio.sleep(0)
        row_dispatched = (
            slot.stage_boundary.stage is None or not slot.stage_boundary.cancellation_hold_refused
        )
        return (0, 0)

    manager.reserve_boundary_cancellation_scopes = _reserve
    manager.cancel_for_boundary = _cancel_boundary
    manager.boundary_cancellation_pending_reason.return_value = reason
    manager.boundary_cancellation_refused.side_effect = lambda _parent, _owner: bool(
        slot.stage_boundary.cancellation_hold_refused
    )
    state.subagents = manager
    mirror = AsyncMock()
    monkeypatch.setattr(orchestrator, "_deliver_halt_notice_to_channels", mirror)
    controller_started = asyncio.Event()

    async def _controller() -> None:
        controller_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            slot._in_stage_execution = False
            order.append("controller-finished")
            await orchestrator._release_cancelled_plan_boundary(
                state,
                slot,
                controller_active=True,
            )

    controller = asyncio.create_task(_controller())
    slot._in_stage_execution = True
    slot.track_stage_controller(controller)
    slot.task = controller
    await controller_started.wait()

    async with TestClient(TestServer(_make_app(state))) as client:
        await _cancel(client, slot.key)
    await asyncio.sleep(0)
    if state._background_tasks:
        await asyncio.gather(*tuple(state._background_tasks))

    notice = next(
        row["content"] for row in slot.messages if "overflow count 1" in str(row.get("content", ""))
    )
    assert order == ["reserved", "controller-finished", "swept"]
    assert row_dispatched is False
    assert slot.stage_boundary.stage == 1
    assert slot.stage_boundary.cancellation_hold_refused == reason
    assert "remains blocked" in notice
    mirror.assert_awaited_once_with(state, slot, notice)


@pytest.mark.asyncio
async def test_active_stage_cancel_releases_boundary_and_hands_off_once(tmp_path, monkeypatch):
    """Active plan Cancel joins, releases, reports, and hands off once."""
    import kiro_crew.dashboard.chat_orchestrator as orchestrator

    state, slot = _make_orchestrator_state(tmp_path, "active-cancel", ["First"])
    slot.stage_boundary.arm(1, consumed=True)
    slot.queue_append("after cancel")
    controller_started = asyncio.Event()
    handed_off: list[str] = []
    order: list[str] = []

    async def _controller() -> None:
        controller_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            slot._in_stage_execution = False
            order.append("controller-finished")

    async def _start_next(_state, _slot) -> bool:
        handed_off.append(_slot.queue_pop(0)["content"])
        order.append("handoff")
        return True

    controller = asyncio.create_task(_controller())
    slot._in_stage_execution = True
    slot.track_stage_controller(controller)
    slot.task = controller
    await controller_started.wait()

    append_and_surface = orchestrator.append_and_surface

    def _record_report(*args, **kwargs):
        if len(args) > 3 and "Plan cancelled" in str(args[3]):
            order.append("cancel-reported")
        return append_and_surface(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "append_and_surface", _record_report)
    monkeypatch.setattr(orchestrator, "_start_next_queued_turn", _start_next)
    try:
        async with TestClient(TestServer(_make_app(state))) as client:
            await _cancel(client, slot.key)
        assert order == ["controller-finished", "cancel-reported", "handoff"]
        assert handed_off == ["after cancel"]
        assert slot.stage_boundary.stage is None
        assert slot._queue == []
        assert controller.done()
    finally:
        if not controller.done():
            controller.cancel()
            await asyncio.gather(controller, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_holds_admission_until_settlement_and_terminal_broadcast(
    tmp_path, monkeypatch
):
    """Repeat Cancel cannot release early; the first queued turn hands off last."""
    state, slot = _make_orchestrator_state(tmp_path, "cancel-admission", ["First"])
    _arm_owned_stage_delivery(slot)
    settle_started, finish_settle = asyncio.Event(), asyncio.Event()
    order: list[str] = []

    async def _settle(_agent_ids: list[str]) -> None:
        order.append("settle-start")
        settle_started.set()
        await finish_settle.wait()
        order.append("settle-end")

    async def _run_after_cancel(*_args, **_kwargs) -> None:
        order.append("handoff")

    state.subagents.settle_queued_delivery = _settle
    monkeypatch.setattr(
        state,
        "broadcast_ws",
        lambda event, _payload: order.append("chat_done") if event == "chat_done" else None,
    )
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", _run_after_cancel)
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _run_after_cancel)

    async with TestClient(TestServer(_make_app(state))) as client:
        cancel_path = f"/api/chat/slots/{slot.key}/plan-action"
        cancel_task = asyncio.create_task(client.post(cancel_path, json={"action": "cancel"}))
        await asyncio.wait_for(settle_started.wait(), timeout=1)
        repeat_cancel = asyncio.create_task(client.post(cancel_path, json={"action": "cancel"}))
        await asyncio.sleep(0)
        try:
            assert not repeat_cancel.done()
            response = await client.post(
                "/api/chat?ws=1",
                json={"slot": slot.key, "message": "after cancel"},
            )
            assert response.status == 200 and (await response.json()).get("queued") is True
            assert slot.stage_boundary.stage == 1
            assert "handoff" not in order
        finally:
            finish_settle.set()
            cancel_response, repeat_response = await asyncio.wait_for(
                asyncio.gather(cancel_task, repeat_cancel), timeout=1
            )
        assert cancel_response.status == repeat_response.status == 200

    turn = slot.task
    if isinstance(turn, asyncio.Task):
        await asyncio.wait_for(turn, timeout=1)
    assert order == ["settle-start", "settle-end", "chat_done", "handoff"]
    assert slot.stage_boundary.stage is None
    assert slot._queue == []


@pytest.mark.asyncio
async def test_concurrent_cancels_start_only_one_queued_turn(tmp_path, monkeypatch):
    """A live successor task keeps the second Cancel from handing off again."""
    state, slot = _make_orchestrator_state(tmp_path, "concurrent-cancel", ["First"])
    _arm_owned_stage_delivery(slot)
    slot.queue_append("first queued turn")
    slot.queue_append("second queued turn")
    settle_started = asyncio.Event()
    finish_settle = asyncio.Event()
    release_turns = asyncio.Event()
    started: list[str] = []
    live_tasks: list[asyncio.Task] = []

    async def _settle(_agent_ids: list[str]) -> None:
        settle_started.set()
        await finish_settle.wait()

    async def _start_one(_state, _slot):
        entry = _slot.queue_pop(0)
        started.append(entry["content"])
        task = asyncio.create_task(release_turns.wait())
        live_tasks.append(task)
        _slot.task = task
        return True

    state.subagents.settle_queued_delivery = _settle
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_orchestrator._start_next_queued_turn",
        _start_one,
    )

    async with TestClient(TestServer(_make_app(state))) as client:
        cancel_path = f"/api/chat/slots/{slot.key}/plan-action"
        first_cancel = asyncio.create_task(client.post(cancel_path, json={"action": "cancel"}))
        await asyncio.wait_for(settle_started.wait(), timeout=1)
        second_cancel = asyncio.create_task(client.post(cancel_path, json={"action": "cancel"}))
        for _ in range(100):
            if getattr(slot._lock, "_waiters", None):
                break
            await asyncio.sleep(0)
        assert getattr(slot._lock, "_waiters", None), "repeat Cancel never reached release lock"
        finish_settle.set()
        responses = await asyncio.wait_for(
            asyncio.gather(first_cancel, second_cancel),
            timeout=1,
        )
        assert all(response.status == 200 for response in responses)

    try:
        assert started == ["first queued turn"]
        assert [entry["content"] for entry in slot._queue] == ["second queued turn"]
    finally:
        release_turns.set()
        await asyncio.gather(*live_tasks)


@pytest.mark.asyncio
async def test_new_plan_clears_cancel_latch_and_runs(tmp_path, monkeypatch):
    """Arming a NEW plan clears the latch; the fresh plan runs normally."""
    from kiro_crew.dashboard.chat import _stage_loop
    from kiro_crew.dashboard.chat_title import _reset_auto_run_for_new_plan

    state, slot = _make_orchestrator_state(tmp_path, "cancel-then-replan", ["First"])

    stages_run: list[int] = []

    async def _mock_run_chat(_state, _slot, _message, **_kwargs):
        stages_run.append(len(stages_run) + 1)
        _slot.append("assistant", f"stage {len(stages_run)} body", "msg msg-a")

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        await _cancel(client, "cancel-then-replan")
        assert slot._plan_cancelled is True

        # The plan detector arms a new plan through this reset — the ONLY site
        # that clears the latch (a bare Go must not).
        _reset_auto_run_for_new_plan(slot)
        slot._stage_titles = ["Fresh stage"]
        slot._auto_run = True
        assert slot._plan_cancelled is False

        await asyncio.wait_for(_stage_loop(state, slot, auto_run=True), timeout=5)

    assert stages_run == [1], f"freshly armed plan should run its stage, ran {stages_run}"


@pytest.mark.asyncio
async def test_cancelled_early_exit_hands_off_queued_message(tmp_path, monkeypatch):
    """A message queued during the race window is dispatched, not stranded.

    Go creates the pending loop and sets ``slot.task`` (so ``api_chat`` queues
    incoming messages), the user types one, then Cancel wins the race. The
    early exit must mirror the loop ``finally``'s queued-work handoff — both
    review lanes flagged the original early return for stranding that message
    until the user's next turn.
    """
    from kiro_crew.dashboard.chat import _stage_loop

    state, slot = _make_orchestrator_state(tmp_path, "cancel-queued", ["First", "Second"])

    handed_off: list[object] = []

    async def _mock_start_next_queued_turn(_state, _slot):
        handed_off.append(_slot._queue.pop(0) if _slot._queue else None)
        return True

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_orchestrator._start_next_queued_turn",
        _mock_start_next_queued_turn,
    )

    async def _mock_run_chat(_state, _slot, _message, **_kwargs):
        raise AssertionError("cancelled plan must not run a stage")

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        await _cancel(client, "cancel-queued")
        # A second Go click racing the Cancel: the plan-action handler queues a
        # kind="plan_approval" entry when the slot is busy. It approves the
        # revoked plan and must be dropped, not dispatched (GPT CI finding).
        # Enqueued through the REAL handler so this test also pins that the
        # handler tags approvals structurally rather than as bare content
        # (Design review finding).
        slot.task = asyncio.get_running_loop().create_future()  # busy → handler queues
        resp = await client.post("/api/chat/slots/cancel-queued/plan-action", json={"action": "go"})
        assert resp.status == 200 and (await resp.json()).get("queued") is True
        assert (
            slot._queue and slot._queue[0].get("kind") == "plan_approval"
        ), "handler must tag queued approvals structurally, not as bare content"
        slot.task = None
        # An untagged typed "go" is a PLAIN user message at drain time — it
        # must be preserved and handed off, not deleted (content matching
        # deletes linked Slack users' real messages).
        slot.queue_append("go")
        # The message the user typed while the Go POST was in flight.
        slot.queue_append("follow-up while plan pending")

        loop_task = asyncio.create_task(_stage_loop(state, slot, auto_run=True))
        slot.task = loop_task
        await asyncio.wait_for(loop_task, timeout=5)

    assert len(handed_off) == 1, "queued message was stranded by the cancelled early exit"
    assert handed_off[0] is not None and handed_off[0]["content"] == "go", (
        "the tagged approval must be dropped; the untagged typed 'go' is a plain "
        f"user message and hands off first: {handed_off}"
    )
    assert [e["content"] for e in slot._queue] == ["follow-up while plan pending"]
    assert slot._orch_tracker is None


@pytest.mark.asyncio
async def test_mid_loop_cancel_drops_queued_approval_at_finally_drain(tmp_path, monkeypatch):
    """A Go queued while the plan ran must not drain after a mid-loop cancel.

    The loop ``finally`` hands off queued work; without filtering, a
    kind="plan_approval" entry queued mid-plan would dispatch through
    ``_run_chat`` after the cancel — the residual both advisory lanes flagged.
    A real user message queued alongside must still be handed off.
    """
    from kiro_crew.dashboard.chat import _stage_loop

    state, slot = _make_orchestrator_state(tmp_path, "cancel-finally-drain", ["First", "Second"])

    entered = asyncio.Event()
    release = asyncio.Event()
    stages_run: list[int] = []
    handed_off: list[object] = []

    async def _mock_run_chat(_state, _slot, _message, **_kwargs):
        stages_run.append(len(stages_run) + 1)
        _slot.append("assistant", f"stage {len(stages_run)} body", "msg msg-a")
        if len(stages_run) == 1:
            entered.set()
            await release.wait()

    async def _mock_start_next_queued_turn(_state, _slot):
        handed_off.append(_slot._queue.pop(0) if _slot._queue else None)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_orchestrator._start_next_queued_turn",
        _mock_start_next_queued_turn,
    )

    async with TestClient(TestServer(_make_app(state))) as client:
        loop_task = asyncio.create_task(_stage_loop(state, slot, auto_run=True))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            # Queued while the plan runs: a tagged button approval (dropped)
            # and an untagged typed "go all" — a PLAIN user message at drain
            # time, preserved (content matching is data loss).
            slot.queue_append("Go", kind="plan_approval")
            slot.queue_append("go all")
            slot.queue_append("real message during plan")
            await _cancel(client, "cancel-finally-drain")
        finally:
            release.set()
        await asyncio.wait_for(loop_task, timeout=5)

    assert stages_run == [1]
    assert len(handed_off) == 1 and handed_off[0]["content"] == "go all", (
        "finally drain must drop only the tagged approval; the untagged 'go all' "
        f"is a plain message and hands off first: {handed_off}"
    )
    assert [e["content"] for e in slot._queue] == ["real message during plan"]


@pytest.mark.asyncio
async def test_cancelled_plan_delivers_unrelated_completion(tmp_path, monkeypatch):
    """Cancel drops only this plan's work, not an earlier agent's result."""
    from kiro_crew.dashboard.chat import _stage_loop
    from kiro_crew.dashboard.chat_utils import SUBAGENT_COMPLETION_KIND

    state, slot = _make_orchestrator_state(tmp_path, "cancel-unrelated-result", ["First"])
    entered = asyncio.Event()
    release = asyncio.Event()
    handed_off: list[dict] = []
    unrelated = "[Subagent completion event]\nresult from an earlier turn"
    owned = "[Subagent completion event]\nresult from this stage"

    async def _mock_run_chat(_state, _slot, _message, **_kwargs):
        entered.set()
        await release.wait()

    async def _mock_start_next_queued_turn(_state, _slot):
        handed_off.append(_slot.queue_pop(0))
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_orchestrator._start_next_queued_turn",
        _mock_start_next_queued_turn,
    )

    async with TestClient(TestServer(_make_app(state))) as client:
        loop_task = asyncio.create_task(_stage_loop(state, slot, auto_run=True))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            # This agent started before the plan. Its completion happens to land
            # while Autopilot is active, but the cancelled plan does not own it.
            owner = slot.stage_boundary.owner
            assert owner is not None
            slot.queue_append(
                unrelated,
                kind=SUBAGENT_COMPLETION_KIND,
                meta=slot.stage_boundary.tag_meta({}, owner=""),
            )
            slot.queue_append(
                owned,
                kind=SUBAGENT_COMPLETION_KIND,
                meta=slot.stage_boundary.tag_meta({}, owner=owner),
            )
            await _cancel(client, slot.key)
        finally:
            release.set()
        await asyncio.wait_for(loop_task, timeout=5)

    assert [entry["content"] for entry in handed_off] == [unrelated], (
        "plan cancellation discarded an unrelated agent completion instead of "
        "delivering it as the next ordinary queued turn"
    )
    assert slot._queue == [], "the cancelled stage's owned completion survived cancellation"


def test_is_plan_approval_entry_matches_tag_only():
    """Only the structural tag matches; untagged content is NEVER dropped.

    An untagged "go" in the queue is a plain user message (e.g. a linked
    Slack user's text) — deleting it is data loss. It is also harmless to
    keep: a drained entry dispatches through _run_chat as an ordinary turn,
    never re-entering api_chat's typed-go branch, and the stage-loop latch
    blocks advancement on a cancelled plan regardless.
    """
    from kiro_crew.dashboard.chat_orchestrator import _is_plan_approval_entry

    assert _is_plan_approval_entry({"content": "Go", "kind": "plan_approval"})
    assert not _is_plan_approval_entry({"content": "go", "kind": ""})
    assert not _is_plan_approval_entry({"content": "Go All", "kind": ""})
    assert not _is_plan_approval_entry({"content": "real message", "kind": ""})
    assert not _is_plan_approval_entry({"content": "go", "kind": "synthetic_recovery"})


@pytest.mark.asyncio
async def test_stop_word_cancel_also_sets_latch(tmp_path):
    """The typed stop-word surface revokes with the same finality as Cancel.

    ``api_chat``'s stop-word branch that calls only ``tracker.stop()`` is not
    enough: the Slack gateway can lazily re-create a fresh unstopped tracker on
    the slot, after which a later Go passes a tracker-only check and resurrects
    the stopped plan. Both cancel surfaces must set the latch.
    """
    from kiro_crew.context_management import MAX_STAGE_ROUNDS, OrchestrationTracker

    state, slot = _make_orchestrator_state(tmp_path, "stop-word", ["First", "Second"])
    tracker = OrchestrationTracker(stage_timeout_seconds=60)
    # has_escalated is derived: a stage at its round limit is the escalated state
    # in which the stop-word branch is reachable.
    tracker._stage_rounds[1] = MAX_STAGE_ROUNDS
    slot._orch_tracker = tracker

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat", json={"slot": "stop-word", "message": "stop"})
        assert resp.status == 200
        assert (await resp.json()).get("stopped") is True

    assert tracker.stopped is True
    assert slot._plan_cancelled is True, "stop-word cancel must set the same latch as Cancel"


@pytest.mark.asyncio
async def test_normal_cancel_of_running_plan_still_stops_it(tmp_path, monkeypatch):
    """The pre-existing path: cancel mid-``_run_chat`` still stops the plan."""
    from kiro_crew.dashboard.chat import _stage_loop

    state, slot = _make_orchestrator_state(tmp_path, "cancel-running", ["First", "Second"])

    entered = asyncio.Event()
    release = asyncio.Event()
    stages_run: list[int] = []

    async def _mock_run_chat(_state, _slot, _message, **_kwargs):
        stages_run.append(len(stages_run) + 1)
        _slot.append("assistant", f"stage {len(stages_run)} body", "msg msg-a")
        if len(stages_run) == 1:
            entered.set()
            await release.wait()

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        loop_task = asyncio.create_task(_stage_loop(state, slot, auto_run=True))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            await _cancel(client, "cancel-running")

            tracker = slot._orch_tracker
            assert tracker is not None and tracker.stopped is True
            assert slot._plan_cancelled is True
            assert slot._auto_run is False
        finally:
            # An assertion failure above must not leave the mocked _run_chat
            # parked in release.wait() ("Task was destroyed but it is pending").
            release.set()
        await asyncio.wait_for(loop_task, timeout=5)

    assert stages_run == [1], f"cancel of a running plan still advanced: ran {stages_run}"
    assert _cancelled_rows(slot) == 1
