"""A plan cancel must stop the stage loop, whatever the loop is awaiting.

The Cancel control (``api_chat_plan_action`` with ``action="cancel"``) revokes the
user's approval to keep orchestrating: it calls ``tracker.stop()`` and clears
``slot._auto_run``. It deliberately does NOT set ``slot._stopping`` -- that flag
carries session/ACP teardown, which a plan cancel is not asking for.

``_stage_loop``'s advancement checks read only ``slot._stopping``, so none of them
observe the cancel. The window that matters is ``_run_chat``: it is the longest
await in the loop, so it is where a cancel most often lands, and the loop resumes
from it and runs the next stage against a revoked approval.

These tests drive the real HTTP cancel handler concurrently with a real
``_stage_loop`` and assert the loop stops. Each also asserts ``slot._stopping``
stays False, pinning the fix to reading the tracker rather than to widening what
a plan cancel means.
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
    ``running_agents_for`` returning None, breaks out of the loop on its own. A
    test that left either unset would see the loop stop for that reason and pass
    without the cancel doing anything -- so this wires the permissive case, where
    the ONLY thing that can stop the loop is the cancel.
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


class _AsyncioFastSleep:
    """The real ``asyncio`` module with only ``sleep`` substituted.

    Replacing ``asyncio.sleep`` itself would reach every coroutine in the process
    for the duration of the test -- including the aiohttp client the cancel is
    issued through, which the substitute itself awaits. Rebinding the module
    reference ``chat_orchestrator`` holds keeps the substitution to the one call
    site under test; every other attribute (``to_thread``, ``create_task``)
    resolves to the real module.
    """

    def __init__(self, sleep):
        self.sleep = sleep

    def __getattr__(self, name):
        return getattr(asyncio, name)


async def _cancel(client, slot_key):
    resp = await client.post(f"/api/chat/slots/{slot_key}/plan-action", json={"action": "cancel"})
    assert resp.status == 200
    assert (await resp.json())["cancelled"] is True


@pytest.mark.asyncio
async def test_cancel_during_run_chat_does_not_advance(tmp_path, monkeypatch):
    """Cancel while stage 1 is inside ``_run_chat`` -- stage 2 must never run."""
    from kiro_crew.dashboard.chat import _stage_loop

    state, slot = _make_orchestrator_state(tmp_path, "cancel-mid-chat", ["First", "Second"])

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
        # Suspend the loop exactly inside _run_chat, then cancel from outside.
        await asyncio.wait_for(entered.wait(), timeout=5)
        await _cancel(client, "cancel-mid-chat")

        tracker = slot._orch_tracker
        assert tracker is not None and tracker.stopped is True
        assert slot._auto_run is False
        assert slot._stopping is False, (
            "plan cancel must not claim session teardown -- the loop is expected "
            "to observe the tracker, not this flag"
        )

        release.set()
        await asyncio.wait_for(loop_task, timeout=5)

    assert stages_run == [1], f"cancel landed inside _run_chat but the loop ran {stages_run}"


@pytest.mark.asyncio
async def test_cancel_between_stages_blocks_reentry(tmp_path, monkeypatch):
    """A cancel taken between stages must stop the re-entered loop at the top.

    Under manual "Go" each stage is its own ``_stage_loop`` entry: the loop runs
    one stage, emits the approval prompt and returns, and the tracker carries the
    progress to the next entry. A cancel taken while the slot sits idle at that
    prompt has to be observed by the top-of-iteration check when the user's next
    Go re-enters -- so this runs stage 1 for real first, which is also what puts
    a tracker on the slot for Cancel to stop.
    """
    from kiro_crew.dashboard.chat import _stage_loop

    state, slot = _make_orchestrator_state(tmp_path, "cancel-between", ["First", "Second"])
    slot._auto_run = False

    stages_run: list[int] = []

    async def _mock_run_chat(_state, _slot, _message, **_kwargs):
        stages_run.append(len(stages_run) + 1)
        _slot.append("assistant", f"stage {len(stages_run)} body", "msg msg-a")

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        # First Go: runs stage 1, then pauses for approval and returns.
        await asyncio.wait_for(_stage_loop(state, slot, auto_run=False), timeout=5)
        assert stages_run == [1], "setup: the first Go should have run exactly stage 1"

        await _cancel(client, "cancel-between")
        assert slot._orch_tracker.stopped is True
        assert slot._stopping is False

        # A later Go re-enters the loop, which resumes at stage 2.
        await asyncio.wait_for(_stage_loop(state, slot, auto_run=False), timeout=5)

    assert stages_run == [1], f"cancelled plan still advanced: ran {stages_run}"


@pytest.mark.asyncio
async def test_cancel_during_subagent_wait_does_not_advance(tmp_path, monkeypatch):
    """Cancel while the loop polls for pending subagents -- stage 2 must not run.

    The poll is the loop's other long await. Its sleep is replaced so the poll
    turns over immediately, and the cancel is issued from inside that sleep --
    i.e. exactly between two evaluations of the poll's own stop condition, which
    is what makes the timing deterministic rather than a race against a 2s tick.
    """
    from kiro_crew.dashboard import chat_orchestrator
    from kiro_crew.dashboard.chat import _stage_loop

    state, slot = _make_orchestrator_state(tmp_path, "cancel-subagent", ["First", "Second"])
    # Never drains on its own: only the cancel can end this wait.
    state.subagents.running_agents_for = MagicMock(return_value=[{"id": "sa-1"}])

    stages_run: list[int] = []

    async def _mock_run_chat(_state, _slot, _message, **_kwargs):
        stages_run.append(len(stages_run) + 1)
        _slot.append("assistant", f"stage {len(stages_run)} body", "msg msg-a")

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        polls = {"n": 0}
        real_sleep = asyncio.sleep

        async def _fast_sleep(_delay, *args, **kwargs):
            polls["n"] += 1
            if polls["n"] == 1:
                await _cancel(client, "cancel-subagent")
            elif polls["n"] > 10:
                raise AssertionError("subagent poll kept spinning after the plan was cancelled")
            await real_sleep(0)

        monkeypatch.setattr(
            chat_orchestrator, "asyncio", _AsyncioFastSleep(_fast_sleep), raising=True
        )
        await asyncio.wait_for(_stage_loop(state, slot, auto_run=True), timeout=5)

    assert slot._stopping is False
    assert stages_run == [1], f"cancel landed in the subagent wait but the loop ran {stages_run}"
