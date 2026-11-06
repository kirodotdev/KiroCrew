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


# ── Inspections: only admitted runs, never more in flight than the mirror holds ──


class _SlowInspect(FakeRuntime):
    """A runtime whose ``inspect`` answers only once *gate* is set."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.inspected: list[str] = []

    async def extension_request(self, method, params, *, timeout=30.0):
        if method == wire.METHOD_INSPECT:
            self.inspected.append(params["workflowId"])
            await self.gate.wait()
            return {}
        return await super().extension_request(method, params, timeout=timeout)


@pytest.mark.asyncio
async def test_a_run_the_mirror_refuses_is_never_inspected(monkeypatch):
    router = make_router(audit=lambda **_k: None)
    rt = _SlowInspect()
    sink = router.sink_for(rt)
    monkeypatch.setattr(mirror_mod, "MAX_LIVE_RUNS", 1)
    sink.on_notification(wire.NOTIFY_RUN_START, {"workflowId": "live-1", "nodeTree": []})
    sink.on_notification(wire.NOTIFY_RUN_START, {"workflowId": "refused", "nodeTree": []})
    await async_wait_until(lambda: rt.inspected == ["live-1"])
    assert router._inspecting == {"live-1"}
    rt.gate.set()
    await async_wait_until(lambda: not router._inspecting)


@pytest.mark.asyncio
async def test_inspections_in_flight_are_capped(monkeypatch):
    from kiro_crew.kas_workflows import router as router_mod

    monkeypatch.setattr(router_mod, "MAX_ROUTED_RUNS", 2)
    router = make_router(audit=lambda **_k: None)
    rt = _SlowInspect()
    sink = router.sink_for(rt)
    for n in range(4):
        sink.on_notification(wire.NOTIFY_RUN_START, {"workflowId": f"w{n}", "nodeTree": []})
    await async_wait_until(lambda: len(rt.inspected) == 2)
    assert len(router._inspecting) == 2
    rt.gate.set()
    await async_wait_until(lambda: not router._inspecting)


# ── A held approval keeps nothing past its caps ──


async def _held(router, rt, params) -> dict:
    sink = router.sink_for(rt)
    sink.on_notification(
        wire.NOTIFY_RUN_START,
        {"workflowId": "w0", "nodeTree": [{"type": "step", "id": "a", "label": "Do"}]},
    )
    sink.on_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "w0", "nodeId": "a", "type": "step", "sessionId": "s0"},
    )
    sink.on_step_request(7, "s0", PERM, params)
    await async_wait_until(lambda: router.approvals())
    return router._pending[router.approvals()[0]["key"]]


def _ask(**extra) -> dict:
    return {
        "sessionId": "s0",
        "toolCall": {"title": "Run it", "rawInput": {}},
        "options": [
            {"optionId": "yes", "kind": "allow_once", "name": "Allow"},
            {"optionId": "always", "kind": "allow_always", "name": "Always"},
            {"optionId": "no", "kind": "reject_once", "name": "Reject"},
        ],
        **extra,
    }


@pytest.mark.asyncio
async def test_a_huge_title_is_kept_capped_and_gets_no_grant():
    from kiro_crew.kas_workflows import router_base

    router = make_router(audit=lambda **_k: None)
    huge = "t" * (router_base._MAX_DISPLAY_CHARS * 50)
    approval = await _held(router, FakeRuntime(), _ask(toolCall={"title": huge, "rawInput": {}}))
    assert len(approval.raw_title) == router_base._MAX_DISPLAY_CHARS
    assert len(approval.title) <= router_base._MAX_DISPLAY_CHARS
    assert approval.grant_key is None


@pytest.mark.asyncio
async def test_options_are_capped_and_an_over_long_id_is_dropped():
    from kiro_crew.kas_workflows import asks, router_base

    router = make_router(audit=lambda **_k: None)
    many = [
        {"optionId": f"o{n}", "kind": "allow_once", "name": "x" * 10_000}
        for n in range(router_base._MAX_OFFERED_OPTIONS * 4)
    ]
    long_id = {"optionId": "i" * (asks.MAX_ID_CHARS + 1), "kind": "reject_once", "name": "No"}
    approval = await _held(router, FakeRuntime(), _ask(options=[long_id, *many]))
    assert len(approval.options) == router_base._MAX_OFFERED_OPTIONS - 1
    assert all(len(o["optionId"]) <= asks.MAX_ID_CHARS for o in approval.options)
    assert all(len(o["name"]) <= 200 for o in approval.options)


@pytest.mark.asyncio
async def test_a_plan_is_kept_as_its_capped_view_not_the_raw_payload():
    router = make_router(audit=lambda **_k: None)
    plan = {
        "kind": "k" * 100_000,
        "omitted": "o" * 100_000,
        "keptStepIds": ["s"] * 100_000,
        "steps": [{"id": f"n{i}", "type": "step", "label": "L"} for i in range(5000)],
        "junk": "j" * 1_000_000,
    }
    params = _ask(_meta={"kiro": {"toolId": wire.TOOL_UPDATE_WORKFLOW, "workflowPlan": plan}})
    approval = await _held(router, FakeRuntime(), params)
    kept = approval.plan
    assert "junk" not in kept
    assert len(kept["kind"]) <= 500 and len(kept["omitted"]) <= 500
    assert len(kept["keptStepIds"]) <= 400
    assert len(repr(kept)) < 200_000


@pytest.mark.asyncio
async def test_an_over_long_tool_id_or_command_gets_no_grant():
    from kiro_crew.kas_workflows import asks, router_base

    router = make_router(audit=lambda **_k: None)
    long_tool = _ask(_meta={"kiro": {"toolId": "x" * (asks.MAX_ID_CHARS + 1)}})
    assert (await _held(router, FakeRuntime(), long_tool)).grant_key is None
    router = make_router(audit=lambda **_k: None)
    long_cmd = _ask(
        toolCall={
            "title": "Run",
            "rawInput": {"command": "e" * (router_base._MAX_GRANT_COMMAND_CHARS + 1)},
        }
    )
    assert (await _held(router, FakeRuntime(), long_cmd)).grant_key is None


def test_question_ids_over_the_cap_are_not_kept():
    from kiro_crew.kas_workflows import asks

    common = dict(
        key="k",
        request_id=1,
        session_id="s",
        owner=("w", "n"),
        created_at=0.0,
        runtime=None,
        path=None,
        slot=None,
    )
    long_id = "q" * (asks.MAX_ID_CHARS + 1)
    legacy = asks.question_from_request(
        {
            "toolCall": {"toolCallId": long_id, "title": "Pick"},
            "options": [{"optionId": long_id, "kind": "allow_once"}]
            + [{"optionId": f"o{n}", "kind": "allow_once"} for n in range(500)],
        },
        **common,
    )
    assert legacy is not None and legacy.question_id == "question"
    assert len(legacy.options) <= 50
    assert all(len(o.option_id) <= asks.MAX_ID_CHARS for o in legacy.options)
    rich = asks.question_from_user_input({"toolCallId": long_id, "question": "Q"}, **common)
    assert rich.question_id == "question"


def test_reconnect_messages_waiting_are_capped():
    from kiro_crew.kas_workflows import router_base

    router = make_router(audit=lambda **_k: None)
    for n in range(router_base.MAX_PENDING_LAUNCHES * 3):
        router.prepare_reconnect(f"chat-{n}")
    assert len(router._reconnects) <= router_base.MAX_PENDING_LAUNCHES


# ── Ids are kept whole or not at all; launch fields are refused past their caps ──


def test_plan_node_sources_and_join_policy_are_capped():
    from kiro_crew.kas_workflows import asks

    node = {
        "id": "n1",
        "type": "step",
        "modelSource": "m" * 100_000,
        "effortSource": "e" * 100_000,
        "joinPolicy": "j" * 100_000,
    }
    (kept,) = asks.plan_nodes({"steps": [node]}) or []
    assert all(len(kept[k]) <= 500 for k in ("modelSource", "effortSource", "joinPolicy"))


def test_the_mirror_refuses_a_run_id_over_its_cap_instead_of_cutting_it():
    long_id = "w" * (mirror_mod.MAX_RUN_ID_CHARS + 1)
    held = mirror_mod.RunMirror()
    assert held.apply_notification(wire.NOTIFY_RUN_START, {"workflowId": long_id}) is None
    assert held.apply_load({"workflowId": long_id, "state": {"status": "running"}}) is None
    summary = wire.RunSummary(workflow_id=long_id, status="running", name="x")
    assert held.apply_summaries([summary]) == []
    assert held.apply_scoped_list("/proj", [summary]) == ([], [])
    assert len(held) == 0
    assert held.dropped == 4


@pytest.mark.asyncio
async def test_a_request_id_over_the_cap_is_answered_and_never_held():
    from kiro_crew.kas_workflows import asks

    router = make_router(audit=lambda **_k: None)
    rt = FakeRuntime()
    sink = router.sink_for(rt)
    _finish_run(sink, 0)
    sink.on_notification(wire.NOTIFY_RUN_START, {"workflowId": "w0", "nodeTree": []})
    long_id = "r" * (asks.MAX_ID_CHARS + 1)
    sink.on_step_request(long_id, "s0", PERM, _ask(sessionId="s0"))
    sink.on_step_request(long_id + "q", "s0", "_kiro/userInput", {"question": "Q"})
    await async_wait_until(lambda: len(rt.responses) == 2)
    assert router._pending == {} and router._questions == {}
    # The two admissions run concurrently, so they may answer in either order.
    answers = dict(rt.responses)
    assert answers[long_id + "q"]["action"] == "answered"
    assert "outcome" in answers[long_id]


@pytest.mark.asyncio
async def test_an_observe_for_an_over_long_parent_is_not_queued():
    from kiro_crew.kas_workflows import asks

    router = make_router(audit=lambda **_k: None)
    router._turn_starter = lambda *a, **k: True
    long_parent = "p" * (asks.MAX_ID_CHARS + 1)
    router._parent_slot[long_parent] = "chat-1"
    router._parent_slot["acp-parent"] = "chat-1"
    assert router._queue_observe("w1", long_parent, "completed", {}) is False
    assert router._observes == {}
    # A parent within the cap still is.
    assert router._queue_observe("w1", "acp-parent", "completed", {}) is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": "s" * (512 * 1024 + 1)},
        {"cwd": "/" + "c" * 5000},
        {"run_label": "l" * 5000},
        {"model": "m" * 5000},
        {"effort": "e" * 5000},
        {"inputs": {f"k{n}": "v" for n in range(65)}},
        {"inputs": {"k": "v" * 5000}},
    ],
)
def test_a_launch_over_its_caps_is_refused(kwargs):
    from kiro_crew.kas_workflows.router import KasWorkflowError

    router = make_router(audit=lambda **_k: None)
    args = {"cwd": "/w", "source": "bundled://x", "inputs": {}}
    args.update(kwargs)
    with pytest.raises(KasWorkflowError):
        router.prepare_launch(
            "chat-1",
            args.pop("cwd"),
            args.pop("source"),
            args.pop("inputs"),
            **args,
        )
    assert router._launches == {}


@pytest.mark.asyncio
async def test_kas_naming_a_new_run_by_an_over_long_id_is_refused(tmp_path):
    from kiro_crew.kas_workflows.router import KasWorkflowError

    router = make_router(audit=lambda **_k: None)
    rt = FakeRuntime()
    rt.answers[wire.METHOD_NEW] = {"workflowId": "w" * (mirror_mod.MAX_RUN_ID_CHARS + 1)}
    launch = router.prepare_launch("chat-1", str(tmp_path), "bundled://x", {})
    with pytest.raises(KasWorkflowError, match="too long"):
        await router.start_in_session(rt, "acp-parent", launch)
    assert router._ledger == {} and router._run_runtime == {}


def test_reconnect_tries_of_chats_the_ledger_no_longer_names_are_dropped():
    router = make_router(audit=lambda **_k: None)
    router._reconnect_tries["chat-gone"] = (1, 0.0)
    router._ledger["w1"] = {"slot": "chat-1", "cwd": "", "createdAt": 1.0, "ended": True}
    router.due_reconnects()
    assert "chat-gone" not in router._reconnect_tries
