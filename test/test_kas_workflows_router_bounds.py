"""The router's routes stay as bounded as the run mirror, and step asks share the answer cap.

A route (``_run_runtime``, ``_step_runtime``) is kept only for a run the mirror holds,
and goes when the mirror evicts the run. A step's permission request is answered by
a task the runtime counts against the same answer capacity as every other answer it
owes, so a backend flooding step asks waits on that cap like any other.
"""

from __future__ import annotations

import asyncio

import pytest
from kas_workflow_helpers import FakeRuntime, make_router
from test_acp_observe_turn import _feed, _session, _stop

from kiro_crew.kas_workflows import mirror as mirror_mod
from kiro_crew.kas_workflows import wire
from kiro_crew.testing.wait import async_wait_until

PERM = "session/request_permission"


def _finish_run(sink, n: int) -> None:
    wid = f"w{n}"
    sink.on_notification(
        wire.NOTIFY_RUN_START,
        {"workflowId": wid, "nodeTree": [{"type": "step", "id": "a", "label": "Do"}]},
    )
    sink.on_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": wid, "nodeId": "a", "type": "step", "sessionId": f"s{n}"},
    )
    sink.on_notification(wire.NOTIFY_RUN_COMPLETE, {"workflowId": wid, "status": "completed"})


@pytest.mark.asyncio
async def test_routes_go_when_the_mirror_evicts_ended_runs():
    router = make_router(audit=lambda **_k: None)
    sink = router.sink_for(FakeRuntime())
    total = mirror_mod.MAX_TERMINAL_RUNS + 50
    for n in range(total):
        _finish_run(sink, n)
    assert len(router.mirror) == mirror_mod.MAX_TERMINAL_RUNS
    assert set(router._run_runtime) == {w for w in router._run_runtime if w in router.mirror}
    assert len(router._run_runtime) == mirror_mod.MAX_TERMINAL_RUNS
    assert set(router._step_runtime) <= router.mirror.step_session_ids()
    assert len(router._step_runtime) == mirror_mod.MAX_TERMINAL_RUNS


@pytest.mark.asyncio
async def test_a_run_the_mirror_refuses_is_not_routed(monkeypatch):
    router = make_router(audit=lambda **_k: None)
    sink = router.sink_for(FakeRuntime())
    monkeypatch.setattr(mirror_mod, "MAX_LIVE_RUNS", 1)
    sink.on_notification(wire.NOTIFY_RUN_START, {"workflowId": "live-1", "nodeTree": []})
    sink.on_notification(wire.NOTIFY_RUN_START, {"workflowId": "refused", "nodeTree": []})
    assert "refused" not in router.mirror
    assert set(router._run_runtime) == {"live-1"}


class _HeldSink:
    """Claims every step request and answers each with a task that waits for *gate*."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.taken: list[int | str] = []

    def claims_step_request(self, session_id: str, method: str) -> bool:
        return True

    def on_step_request(self, request_id, session_id, method, params):
        self.taken.append(request_id)
        return asyncio.create_task(self.gate.wait())

    def __getattr__(self, name):
        return lambda *a, **k: False


def _step_ask(rid: int) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": PERM, "params": {"sessionId": "step-1"}}


@pytest.mark.asyncio
async def test_step_asks_count_against_the_runtimes_answer_cap():
    rt, reader, proc, handle, task = await _session()
    try:
        sink = _HeldSink()
        rt._workflow_sink = sink
        rt._max_answer_tasks = 1
        _feed(reader, _step_ask(1))
        _feed(reader, _step_ask(2))
        await async_wait_until(lambda: sink.taken == [1])
        assert len(rt._answer_tasks) == 1
        # The second ask waits on the cap while the first answer is outstanding.
        for _ in range(50):
            await asyncio.sleep(0)
        assert sink.taken == [1]
        sink.gate.set()
        await async_wait_until(lambda: sink.taken == [1, 2])
    finally:
        sink.gate.set()
        await _stop(task)
