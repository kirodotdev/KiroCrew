"""A cancel racing the first Go must win, and repeat cancels must be idempotent.

``api_chat_plan_action``'s Cancel branch used to revoke a plan only through the
tracker (``tracker.stop()`` when ``slot._orch_tracker`` exists). But the tracker
is created lazily INSIDE ``_stage_loop``, so a Cancel processed in the sub-tick
window between a Go POST being accepted and its ``_stage_loop`` coroutine
running found no tracker, no-opped, appended '🛑 Plan cancelled.' — and the Go
then built a fresh (unstopped) tracker and advanced stage 1. Transcript said
cancelled; plan proceeded (#6046).

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
from unittest.mock import MagicMock

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


@pytest.mark.asyncio
async def test_cancel_before_stage_loop_starts_does_not_advance(tmp_path, monkeypatch):
    """Cancel processed before the stage loop creates a tracker: NO stage runs.

    This is the #6046 window itself: the Go's ``_stage_loop`` task is created
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
        # The #6046 interleaving is "Cancel fully processed before _stage_loop's
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
        # must be preserved and handed off, not deleted (GPT round-6 finding:
        # content matching deletes linked Slack users' real messages).
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
            # time, preserved (GPT round-6: content matching is data loss).
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

    ``api_chat``'s stop-word branch used to call only ``tracker.stop()`` — the
    Slack gateway can lazily re-create a fresh unstopped tracker on the slot,
    after which a later Go would pass a tracker-only check and resurrect the
    stopped plan. Both cancel surfaces must set the latch (Design review
    finding).
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
