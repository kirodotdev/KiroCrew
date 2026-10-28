"""KasWorkflowRouter against fake KAS runtimes: launch, mirror, approvals."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew.kas_workflows import wire
from kiro_crew.kas_workflows.router import (
    KasWorkflowError,
    KasWorkflowRouter,
    StepNotStarted,
    launch_from_turn,
    session_binding,
)
from kiro_crew.testing.wait import async_wait_until


@pytest.fixture(autouse=True)
def _tmp_path_is_a_recent_project(_floor_monkeypatch, tmp_path):
    """These tests use tmp_path as the project, which sits outside the home root.

    Listing it as a recent project keeps the real project-root confinement in
    the path under test instead of switching it off.
    """
    _floor_monkeypatch.setattr(
        "kiro_crew.kas_workflows.project_root._recent_project_dirs", lambda: [str(tmp_path)]
    )


PERM = "session/request_permission"


class FakeRuntime:
    acp_backend = "kas"

    def __init__(self) -> None:
        self.alive = True
        self.calls: list[tuple[str, dict]] = []
        self.responses: list[tuple[Any, dict]] = []
        self.answers: dict[str, Any] = {}

    def is_alive(self) -> bool:
        return self.alive

    async def extension_request(self, method, params, *, timeout=30.0):
        self.calls.append((method, params))
        answer = self.answers.get(method, {})
        return answer(params) if callable(answer) else answer

    async def send_response(self, request_id, result):
        self.responses.append((request_id, result))

    async def kill(self, *, expected=False, reason=""):
        self.alive = False


def _router(**kwargs) -> tuple[KasWorkflowRouter, list[FakeRuntime]]:
    spawned: list[FakeRuntime] = []

    async def factory():
        rt = FakeRuntime()
        spawned.append(rt)
        return rt

    router = KasWorkflowRouter(factory, screen=kwargs.pop("screen", lambda _c: None), **kwargs)
    return router, spawned


def _perm(command: str | None = "ls") -> dict:
    raw = {"command": command} if command is not None else {}
    return {
        "sessionId": "step-1",
        "toolCall": {"title": "Run ls", "rawInput": raw},
        "options": [
            {"optionId": "yes", "kind": "allow_once", "name": "Allow"},
            {"optionId": "always", "kind": "allow_always", "name": "Always"},
            {"optionId": "no", "kind": "reject_once", "name": "Reject"},
        ],
    }


async def _started(tmp_path, router=None):
    if router is None:
        router, _ = _router()
    chat_rt = FakeRuntime()
    chat_rt.answers[wire.METHOD_NEW] = {
        "workflowId": "w1",
        "initialState": {"status": "running", "workflowName": "investigate"},
    }
    sink = router.sink_for(chat_rt)
    launch = router.prepare_launch("chat-1", str(tmp_path), "bundled://investigate", {"q": "why"})
    row = await router.start_in_session(chat_rt, "acp-parent", launch)
    sink.on_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "w1", "nodeId": "a", "type": "step", "sessionId": "step-1"},
    )
    return router, chat_rt, sink, row


class _Inner:
    def __init__(self, runtime, session_id):
        self.runtime = runtime
        self.session_id = session_id


class _Provider:
    def __init__(self, runtime, session_id="acp-parent"):
        self.client = _Inner(runtime, session_id)


# ── Launching ──


@pytest.mark.asyncio
async def test_run_is_parented_by_the_chat_session_on_the_chats_process(tmp_path):
    router, chat_rt, _sink, row = await _started(tmp_path)
    assert [m for m, _ in chat_rt.calls] == [wire.METHOD_NEW, wire.METHOD_INVOKE]
    assert chat_rt.calls[0][1]["parentSessionId"] == "acp-parent"
    assert chat_rt.calls[0][1]["workflowPath"] == "bundled://investigate"
    assert row["slotKey"] == "chat-1"
    assert row["driven"] is True


@pytest.mark.asyncio
async def test_launch_message_round_trips_its_token_and_is_single_use(tmp_path):
    router, _ = _router()
    launch = router.prepare_launch("chat-1", str(tmp_path), "bundled://x", {})
    message = launch.message("investigate")
    assert router.launch_token(message) == launch.token
    assert router.launch_token("Start workflow `x` in `/p`") is None
    assert router.launch_token("hello") is None
    assert router.take_launch("chat-2", launch.token) is None, "another chat cannot take it"
    assert router.take_launch("chat-1", launch.token) == launch
    assert router.take_launch("chat-1", launch.token) is None


@pytest.mark.asyncio
async def test_launch_from_turn_starts_on_the_claimed_provider(tmp_path, monkeypatch):
    from kiro_crew.kas_workflows import router as router_mod

    router, _ = _router()
    monkeypatch.setattr(router_mod, "_ROUTER", router)
    chat_rt = FakeRuntime()
    chat_rt.answers[wire.METHOD_NEW] = {"workflowId": "w9", "initialState": {"status": "running"}}
    launch = router.prepare_launch("chat-1", str(tmp_path), "bundled://x", {})
    reply = await launch_from_turn("chat-1", launch.message("x"), _Provider(chat_rt))
    assert reply is not None and "w9" in reply
    assert chat_rt.calls[0][1]["parentSessionId"] == "acp-parent"
    # Replaying the same message starts nothing.
    again = await launch_from_turn("chat-1", launch.message("x"), _Provider(chat_rt))
    assert again is not None and "expired" in again
    assert [m for m, _ in chat_rt.calls].count(wire.METHOD_NEW) == 1
    assert await launch_from_turn("chat-1", "an ordinary message", _Provider(chat_rt)) is None


@pytest.mark.asyncio
async def test_launch_on_a_non_kas_chat_starts_nothing(tmp_path, monkeypatch):
    from kiro_crew.kas_workflows import router as router_mod

    router, _ = _router()
    monkeypatch.setattr(router_mod, "_ROUTER", router)
    other = FakeRuntime()
    other.acp_backend = "kiro"
    launch = router.prepare_launch("chat-1", str(tmp_path), "bundled://x", {})
    reply = await launch_from_turn("chat-1", launch.message("x"), _Provider(other))
    assert reply is not None and "not running on Kiro Agent" in reply
    assert other.calls == []


def test_session_binding_reads_only_a_kas_provider():
    rt = FakeRuntime()
    assert session_binding(_Provider(rt)) == (rt, "acp-parent")
    assert session_binding(_Provider(rt, session_id="")) is None
    assert session_binding(object()) is None


# ── Notifications and sinks ──


@pytest.mark.asyncio
async def test_sink_claims_only_its_own_step_sessions(tmp_path):
    router, _chat_rt, sink, _ = await _started(tmp_path)
    other_sink = router.sink_for(FakeRuntime())
    assert sink.claims_step_request("step-1", PERM)
    assert not other_sink.claims_step_request("step-1", PERM), "another process's step"
    assert not sink.claims_step_request("acp-parent", PERM)
    # A step's structured question is claimed like a permission request.
    assert sink.claims_step_request("step-1", "_kiro/userInput")
    assert not other_sink.claims_step_request("step-1", "_kiro/userInput")
    assert not sink.claims_step_request("step-1", "_kiro/somethingElse")
    assert sink.claims_notification(wire.NOTIFY_RUN_COMPLETE)
    assert not sink.claims_notification("session/update")


@pytest.mark.asyncio
async def test_a_run_the_chat_model_started_is_adopted_by_its_process(tmp_path):
    router, _ = _router()
    rt = FakeRuntime()
    sink = router.sink_for(rt)
    sink.on_notification(wire.NOTIFY_RUN_START, {"workflowId": "w2", "workflowName": "x"})
    assert router.run("w2")["driven"] is True


@pytest.mark.asyncio
async def test_a_run_a_noted_chats_model_started_belongs_to_that_chat(tmp_path):
    ledger = tmp_path / "runs.json"
    router, _ = _router(ledger_path=ledger)
    rt = FakeRuntime()
    sink = router.sink_for(rt)
    router.note_chat_session("chat-7", _Provider(rt, "acp-chat-7"))
    sink.on_notification(
        wire.NOTIFY_RUN_START,
        {"workflowId": "w3", "workflowName": "x", "parentSessionId": "acp-chat-7"},
    )
    await async_wait_until(lambda: router.slot_of("w3") == "chat-7")
    assert router.run("w3")["slotKey"] == "chat-7"
    await async_wait_until(lambda: ledger.exists() and '"w3"' in ledger.read_text())
    # A later frame of the same run does not re-adopt or move it.
    router.note_chat_session("chat-8", _Provider(rt, "acp-chat-7"))
    sink.on_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "w3", "nodeId": "a", "sessionId": "s", "parentSessionId": "acp-chat-7"},
    )
    assert router.slot_of("w3") == "chat-7"


@pytest.mark.asyncio
async def test_a_run_of_an_unknown_parent_is_not_given_a_chat(tmp_path):
    router, _ = _router()
    rt = FakeRuntime()
    sink = router.sink_for(rt)
    sink.on_notification(
        wire.NOTIFY_RUN_START,
        {"workflowId": "w4", "workflowName": "x", "parentSessionId": "acp-nobody"},
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert router.slot_of("w4") is None


def _inspect_answer(status: str):
    return {
        "workflowId": "w5",
        "state": {"status": status, "workflowName": "investigate"},
        "nodePlan": [{"id": "plan", "type": "step", "label": "Plan"}],
    }


@pytest.mark.asyncio
async def test_a_model_started_run_gets_its_step_tree_read_without_a_load(tmp_path):
    router, _ = _router()
    rt = FakeRuntime()
    rt.answers[wire.METHOD_INSPECT] = _inspect_answer("running")
    sink = router.sink_for(rt)
    sink.on_notification(wire.NOTIFY_RUN_START, {"workflowId": "w5", "workflowName": "x"})
    sink.on_notification(
        wire.NOTIFY_NODE_START, {"workflowId": "w5", "nodeId": "plan", "sessionId": "s"}
    )
    await async_wait_until(lambda: router.mirror.has_plan("w5"))
    methods = [m for m, _ in rt.calls]
    assert methods == [wire.METHOD_INSPECT], "read once, and never load (which takes a run over)"
    # The run's end reads the settled tree once more.
    rt.answers[wire.METHOD_INSPECT] = _inspect_answer("completed")
    sink.on_notification(wire.NOTIFY_RUN_COMPLETE, {"workflowId": "w5", "status": "completed"})
    await async_wait_until(lambda: len(rt.calls) == 2)
    assert [m for m, _ in rt.calls] == [wire.METHOD_INSPECT, wire.METHOD_INSPECT]


@pytest.mark.asyncio
async def test_a_model_started_run_shows_its_run_label_not_the_recipe_name(tmp_path):
    # run_start names the recipe and may carry the whole step tree, but never
    # the run's display label; the one read-only snapshot is what brings it.
    router, _ = _router()
    rt = FakeRuntime()
    answer = _inspect_answer("running")
    answer["workflowId"] = "w7"
    answer["state"]["runLabel"] = "investigate-kiro-docs-crew"
    rt.answers[wire.METHOD_INSPECT] = answer
    sink = router.sink_for(rt)
    sink.on_notification(
        wire.NOTIFY_RUN_START,
        {
            "workflowId": "w7",
            "workflowName": "investigate",
            "nodeTree": [{"nodeId": "investigate", "type": "step", "label": "investigate"}],
        },
    )
    assert router.mirror.has_plan("w7")
    await async_wait_until(lambda: "w7" in router._inspected)
    assert [m for m, _ in rt.calls] == [wire.METHOD_INSPECT], "inspect only; never load"
    row = next(r for r in router.mirror.rows() if r["workflowId"] == "w7")
    assert row["name"] == "investigate-kiro-docs-crew"
    assert row["workflowName"] == "investigate"


@pytest.mark.asyncio
async def test_a_failed_inspect_is_not_retried_on_every_frame(tmp_path):
    router, _ = _router()
    rt = FakeRuntime()

    def boom(_params):
        raise RuntimeError("no such run")

    rt.answers[wire.METHOD_INSPECT] = boom
    sink = router.sink_for(rt)
    sink.on_notification(wire.NOTIFY_RUN_START, {"workflowId": "w6", "workflowName": "x"})
    await async_wait_until(lambda: "w6" in router._inspected)
    for n in range(3):
        sink.on_notification(wire.NOTIFY_NODE_START, {"workflowId": "w6", "nodeId": f"n{n}"})
    for _ in range(5):
        await asyncio.sleep(0)
    assert [m for m, _ in rt.calls] == [wire.METHOD_INSPECT]


def test_note_chat_session_ignores_a_non_kas_provider_and_stays_bounded(monkeypatch):
    from kiro_crew.kas_workflows import router as router_mod

    router, _ = _router()

    class _Kiro(FakeRuntime):
        acp_backend = ""

    router.note_chat_session("chat-1", _Provider(_Kiro(), "acp-x"))
    assert router._parent_slot == {}
    monkeypatch.setattr(router_mod, "MAX_PARENT_SESSIONS", 2)
    rt = FakeRuntime()
    for n in range(3):
        router.note_chat_session(f"chat-{n}", _Provider(rt, f"acp-{n}"))
    assert list(router._parent_slot) == ["acp-1", "acp-2"]


# ── Approvals ──


@pytest.mark.asyncio
async def test_step_permission_is_held_and_answered_on_its_own_process(tmp_path):
    router, chat_rt, sink, _ = await _started(tmp_path)
    sink.on_step_request(11, "step-1", PERM, _perm())
    await async_wait_until(lambda: router.approvals())
    [approval] = router.approvals("w1")
    assert approval["nodeId"] == "a"
    assert approval["command"] == "ls"
    assert chat_rt.responses == [], "nothing is answered before a person decides"
    with pytest.raises(KasWorkflowError):
        await router.answer(approval["key"], "not-offered")
    await router.answer(approval["key"], "yes")
    assert chat_rt.responses == [(11, {"outcome": {"outcome": "selected", "optionId": "yes"}})]
    assert router.approvals() == []


@pytest.mark.asyncio
async def test_denied_command_is_rejected_without_reaching_a_person(tmp_path):
    router, _ = _router(screen=lambda cmd: "denied" if "rm -rf" in cmd else None)
    router, chat_rt, sink, _ = await _started(tmp_path, router)
    sink.on_step_request(12, "step-1", PERM, _perm("rm -rf ~/work"))
    await async_wait_until(lambda: chat_rt.responses)
    assert chat_rt.responses == [(12, {"outcome": {"outcome": "selected", "optionId": "no"}})]
    assert router.approvals() == []


# ── Approvals follow the owning chat's mode ──


async def _no_refusal(_cmd: str):
    return None


def _moded(mode: str, *, name_grant=_no_refusal, audit=None, **kwargs):
    audited: list[dict] = []

    def _audit(**fields):
        if audit is not None:
            audit(**fields)
        audited.append(fields)

    router, _ = _router(
        name_grant=name_grant,
        read_only=lambda cmd: cmd.startswith(("ls", "git log", "cat ")),
        audit=_audit,
        **kwargs,
    )
    reason = {"trust": "trust", "trust_reads": "trust_reads"}.get(mode, "")
    return router, audited, (lambda: (mode, reason))


async def _moded_start(tmp_path, mode: str, **kwargs):
    router, audited, reader = _moded(mode, **kwargs)
    router, chat_rt, sink, _ = await _started(tmp_path, router)
    router.note_chat_session("chat-1", _Provider(chat_rt), reader)
    return router, chat_rt, sink, audited


async def _settled(router, chat_rt):
    await async_wait_until(lambda: chat_rt.responses or router.approvals())


ALLOW_ONCE = {"outcome": {"outcome": "selected", "optionId": "yes"}}


@pytest.mark.asyncio
async def test_a_trusted_chats_step_is_allowed_once_and_logged(tmp_path):
    router, chat_rt, sink, audited = await _moded_start(tmp_path, "trust")
    sink.on_step_request(21, "step-1", PERM, _perm("npm install"))
    await _settled(router, chat_rt)
    assert chat_rt.responses == [(21, ALLOW_ONCE)], "allow ONCE, never always-allow"
    assert router.approvals() == []
    [entry] = audited
    assert entry["session_key"] == "chat-1" and entry["outcome"] == "auto_approved"
    assert entry["metadata"] == {"reason": "trust", "workflowId": "w1", "nodeId": "a"}


@pytest.mark.asyncio
async def test_trust_reads_allows_a_read_and_asks_for_a_change(tmp_path):
    router, chat_rt, sink, audited = await _moded_start(tmp_path, "trust_reads")
    sink.on_step_request(22, "step-1", PERM, _perm("git log -1"))
    await _settled(router, chat_rt)
    assert chat_rt.responses == [(22, ALLOW_ONCE)]
    sink.on_step_request(23, "step-1", PERM, _perm("npm install"))
    await async_wait_until(lambda: router.approvals())
    assert len(chat_rt.responses) == 1, "a change still waits for a person"
    # A tool call with no command (a file write, a plan) is not a read.
    sink.on_step_request(24, "step-1", PERM, _perm(None))
    await async_wait_until(lambda: len(router.approvals()) == 2)
    assert [e["metadata"]["reason"] for e in audited] == ["trust_reads"]


@pytest.mark.parametrize("mode", ["normal", "unknown"])
@pytest.mark.asyncio
async def test_a_normal_chats_step_waits_for_a_person(tmp_path, mode):
    router, chat_rt, sink, audited = await _moded_start(tmp_path, mode)
    sink.on_step_request(25, "step-1", PERM, _perm("ls"))
    await async_wait_until(lambda: router.approvals())
    assert chat_rt.responses == [] and audited == []


@pytest.mark.asyncio
async def test_a_chat_no_turn_has_named_waits_for_a_person(tmp_path):
    router, audited, _reader = _moded("trust")
    router, chat_rt, sink, _ = await _started(tmp_path, router)
    sink.on_step_request(26, "step-1", PERM, _perm("ls"))
    await async_wait_until(lambda: router.approvals())
    assert chat_rt.responses == [] and audited == []


@pytest.mark.asyncio
async def test_the_mode_is_read_when_the_step_asks(tmp_path):
    router, audited, _ = _moded("trust")
    router, chat_rt, sink, _ = await _started(tmp_path, router)
    mode = ["normal"]
    router.note_chat_session("chat-1", _Provider(chat_rt), lambda: (mode[0], mode[0]))
    sink.on_step_request(27, "step-1", PERM, _perm("npm test"))
    await async_wait_until(lambda: router.approvals())
    mode[0] = "trust"
    sink.on_step_request(28, "step-1", PERM, _perm("npm test"))
    await async_wait_until(lambda: chat_rt.responses)
    assert chat_rt.responses == [(28, ALLOW_ONCE)]


@pytest.mark.asyncio
async def test_trust_never_overrides_the_deny_rules(tmp_path):
    router, chat_rt, sink, audited = await _moded_start(
        tmp_path, "trust", screen=lambda cmd: "denied" if "rm -rf" in cmd else None
    )
    sink.on_step_request(29, "step-1", PERM, _perm("rm -rf ~/work"))
    await async_wait_until(lambda: chat_rt.responses)
    assert chat_rt.responses == [(29, {"outcome": {"outcome": "selected", "optionId": "no"}})]
    # Nothing was auto-approved; the refusal itself is audited.
    assert [a["outcome"] for a in audited] == ["auto_rejected"]


@pytest.mark.asyncio
async def test_a_name_grant_refusal_or_a_failed_log_asks_instead(tmp_path):
    async def refuse(_cmd):
        return object()

    def broken_log(**_fields):
        raise OSError("sel down")

    for kwargs in ({"name_grant": refuse}, {"audit": broken_log}):
        router, chat_rt, sink, _ = await _moded_start(tmp_path, "trust", **kwargs)
        sink.on_step_request(30, "step-1", PERM, _perm("npm test"))
        await async_wait_until(lambda: router.approvals())
        assert chat_rt.responses == [], kwargs


@pytest.mark.asyncio
async def test_without_an_allow_once_option_trust_asks(tmp_path):
    router, chat_rt, sink, _ = await _moded_start(tmp_path, "trust")
    perm = _perm("npm test")
    perm["options"] = [o for o in perm["options"] if o["kind"] != "allow_once"]
    sink.on_step_request(31, "step-1", PERM, perm)
    await async_wait_until(lambda: router.approvals())
    assert chat_rt.responses == []


@pytest.mark.asyncio
async def test_a_held_step_follows_its_chat_switching_to_trust(tmp_path):
    router, audited, _ = _moded("trust")
    router, chat_rt, sink, _ = await _started(tmp_path, router)
    mode = ["normal"]
    router.note_chat_session("chat-1", _Provider(chat_rt), lambda: (mode[0], mode[0]))
    sink.on_step_request(40, "step-1", PERM, _perm("npm test"))
    await async_wait_until(lambda: router.approvals())
    assert await router.recheck_pending() == 0, "still Normal: nothing changes"
    assert chat_rt.responses == [] and audited == []
    mode[0] = "trust"
    assert await router.recheck_pending() == 1
    assert chat_rt.responses == [(40, ALLOW_ONCE)]
    assert router.approvals() == []
    assert [e["metadata"]["reason"] for e in audited] == ["trust"]
    assert await router.recheck_pending() == 0


@pytest.mark.asyncio
async def test_recheck_runs_the_checks_once_per_mode_not_every_poll(tmp_path):
    calls = []

    async def refuse(cmd):
        calls.append(cmd)
        return object()

    router, chat_rt, sink, _ = await _moded_start(tmp_path, "trust", name_grant=refuse)
    sink.on_step_request(41, "step-1", PERM, _perm("npm test"))
    await async_wait_until(lambda: router.approvals())
    for _ in range(3):
        await router.recheck_pending()
    assert len(calls) == 1, "the refused step is not re-checked on every poll"
    assert chat_rt.responses == []


PLAN_TEXT = "Steps to add after 'create-workflow':\n\nplan: step, agent wf-planner, model auto"


def _plan_perm(*, with_plan_meta: bool = True, tool_id: str | None = "update_workflow") -> dict:
    perm = _perm(None)
    perm["toolCall"] = {
        "title": "Add 2 workflow steps to w1",
        "content": [{"type": "content", "content": {"type": "text", "text": PLAN_TEXT}}],
    }
    kiro: dict = {}
    if tool_id is not None:
        kiro["toolId"] = tool_id
    if with_plan_meta:
        kiro["workflowPlan"] = {"workflowId": "w1", "kind": "launch", "steps": []}
    perm["_meta"] = {"kiro": kiro}
    return perm


@pytest.mark.parametrize(
    "perm",
    [_plan_perm(), _plan_perm(with_plan_meta=False), _plan_perm(tool_id=None)],
    ids=["tool-and-plan", "too-large-for-plan-meta", "plan-meta-only"],
)
@pytest.mark.asyncio
async def test_a_plan_handoff_always_asks_even_in_trust(tmp_path, perm):
    router, chat_rt, sink, audited = await _moded_start(tmp_path, "trust")
    sink.on_step_request(50, "step-1", PERM, perm)
    await async_wait_until(lambda: router.approvals())
    assert chat_rt.responses == [] and audited == []
    [approval] = router.approvals()
    assert approval["isPlan"] is True
    assert approval["planText"] == PLAN_TEXT
    # A mode change never releases it either.
    assert await router.recheck_pending() == 0
    assert chat_rt.responses == []
    await router.answer(approval["key"], "yes")
    assert chat_rt.responses == [(50, ALLOW_ONCE)]


@pytest.mark.asyncio
async def test_an_ordinary_step_request_is_not_a_plan(tmp_path):
    router, _ = _router()
    router, chat_rt, sink, _ = await _started(tmp_path, router)
    sink.on_step_request(51, "step-1", PERM, _perm("npm test"))
    await async_wait_until(lambda: router.approvals())
    [approval] = router.approvals()
    assert approval["isPlan"] is False and approval["planText"] is None


@pytest.mark.asyncio
async def test_a_full_queue_does_not_stop_a_trusted_step(tmp_path):
    router, chat_rt, sink, _ = await _moded_start(tmp_path, "trust", max_pending=0)
    sink.on_step_request(32, "step-1", PERM, _perm("npm test"))
    await async_wait_until(lambda: chat_rt.responses)
    assert chat_rt.responses == [(32, ALLOW_ONCE)]


@pytest.mark.asyncio
async def test_screen_that_raises_rejects(tmp_path):
    def broken(_cmd: str) -> str | None:
        raise OSError("disk")

    router, _ = _router(screen=broken)
    router, chat_rt, sink, _ = await _started(tmp_path, router)
    sink.on_step_request(13, "step-1", PERM, _perm())
    await async_wait_until(lambda: chat_rt.responses)
    assert chat_rt.responses[0][1]["outcome"]["optionId"] == "no"


@pytest.mark.asyncio
async def test_full_queue_rejects(tmp_path):
    router, _ = _router(max_pending=1)
    router, chat_rt, sink, _ = await _started(tmp_path, router)
    sink.on_step_request(1, "step-1", PERM, _perm())
    await async_wait_until(lambda: router.approvals())
    sink.on_step_request(2, "step-1", PERM, _perm())
    await async_wait_until(lambda: chat_rt.responses)
    assert chat_rt.responses == [(2, {"outcome": {"outcome": "selected", "optionId": "no"}})]


@pytest.mark.asyncio
async def test_approvals_of_a_dead_process_are_dropped(tmp_path):
    router, chat_rt, sink, _ = await _started(tmp_path)
    sink.on_step_request(11, "step-1", PERM, _perm())
    await async_wait_until(lambda: router.approvals())
    key = router.approvals()[0]["key"]
    chat_rt.alive = False
    assert router.approvals() == []
    with pytest.raises(KasWorkflowError):
        await router.answer(key, "yes")


# ── Reads and controls ──


@pytest.mark.asyncio
async def test_runs_reads_disk_and_never_loads_a_run(tmp_path):
    router, spawned = _router()
    rows_answer = {
        "runs": [
            {"workflowId": "live", "name": "a", "workflowName": "a", "status": "running"},
            {"workflowId": "done", "name": "b", "workflowName": "b", "status": "completed"},
        ]
    }

    async def factory():
        rt = FakeRuntime()
        rt.answers[wire.METHOD_LIST] = rows_answer
        spawned.append(rt)
        return rt

    router._query_factory = factory
    rows = await router.runs(str(tmp_path))
    assert {r["workflowId"] for r in rows} == {"live", "done"}
    assert all(not r["driven"] for r in rows)
    [query] = spawned
    assert [m for m, _ in query.calls] == [wire.METHOD_LIST]
    # A second read reuses the query process.
    await router.runs(str(tmp_path))
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_reads_prefer_a_live_chat_process_over_spawning(tmp_path):
    router, spawned = _router()
    chat_rt = FakeRuntime()
    chat_rt.answers[wire.METHOD_LIST_RECIPES] = {"recipes": [{"name": "r", "source": "s"}]}
    router.sink_for(chat_rt)
    recipes = await router.recipes(str(tmp_path))
    assert [r["name"] for r in recipes] == ["r"]
    assert spawned == []


@pytest.mark.asyncio
async def test_control_goes_to_the_driving_process(tmp_path):
    router, chat_rt, _sink, _ = await _started(tmp_path)
    await router.control("w1", "pause", "lunch")
    assert chat_rt.calls[-1] == (
        wire.METHOD_PAUSE,
        {"workflowId": "w1", "initiator": "user", "reason": "lunch"},
    )


@pytest.mark.asyncio
async def test_control_of_an_undriven_live_run_is_refused(tmp_path):
    router, chat_rt, _sink, _ = await _started(tmp_path)
    chat_rt.alive = False
    with pytest.raises(KasWorkflowError):
        await router.control("w1", "cancel")


@pytest.mark.asyncio
async def test_delete_forgets_the_run_and_its_approvals(tmp_path):
    router, chat_rt, sink, _ = await _started(tmp_path)
    sink.on_step_request(11, "step-1", PERM, _perm())
    await async_wait_until(lambda: router.approvals())
    await router.control("w1", "delete")
    assert "w1" not in router.mirror
    assert router.approvals() == []
    assert chat_rt.calls[-1] == (wire.METHOD_DELETE, {"workflowId": "w1", "initiator": "user"})


@pytest.mark.asyncio
async def test_unknown_control_and_missing_project_are_refused(tmp_path):
    router, _chat_rt, _sink, _ = await _started(tmp_path)
    with pytest.raises(KasWorkflowError):
        await router.control("w1", "explode")
    with pytest.raises(KasWorkflowError):
        await router.recipes(str(tmp_path / "nope"))


@pytest.mark.asyncio
async def test_close_kills_only_the_query_process(tmp_path):
    router, spawned = _router()
    chat_rt = FakeRuntime()
    await router.runs(str(tmp_path))  # spawns the query process (no chat yet)
    router.sink_for(chat_rt)
    await router.close()
    assert not spawned[0].alive
    assert chat_rt.alive, "a chat's process belongs to its session"


# ── Ledger and reconnect ──


async def _ledgered(tmp_path):
    """A router with an on-disk ledger and one run launched in chat-1."""
    ledger = tmp_path / "ledger" / "runs.json"
    router, spawned = _router(ledger_path=ledger)
    router, chat_rt, sink, _ = await _started(tmp_path, router)
    return router, chat_rt, sink, ledger


@pytest.mark.asyncio
async def test_ledger_survives_a_gateway_restart(tmp_path):
    router, _chat_rt, _sink, ledger = await _ledgered(tmp_path)
    assert ledger.exists()
    fresh, _ = _router(ledger_path=ledger)
    await fresh.ensure_ledger()
    assert fresh.slot_of("w1") == "chat-1"


@pytest.mark.asyncio
async def test_ledger_marks_a_finished_run_ended(tmp_path):
    router, _chat_rt, sink, ledger = await _ledgered(tmp_path)
    sink.on_notification(wire.NOTIFY_RUN_COMPLETE, {"workflowId": "w1", "status": "completed"})
    await async_wait_until(lambda: '"ended": true' in ledger.read_text())
    assert router._undriven_live() == []


@pytest.mark.asyncio
async def test_unreadable_ledger_starts_empty(tmp_path):
    ledger = tmp_path / "runs.json"
    ledger.write_text("{not json")
    router, _ = _router(ledger_path=ledger)
    await router.ensure_ledger()
    assert router.slot_of("w1") is None


def _loaded(status: str, reason: str | None = None) -> dict:
    state: dict = {"status": status, "workflowName": "investigate"}
    if reason:
        state["pauseReason"] = reason
    return {
        "workflowId": "w1",
        "state": state,
        "stepSessions": [{"nodeId": "a", "nodePath": ["a"], "sessionId": "step-2"}],
    }


@pytest.mark.asyncio
async def test_reattach_resumes_a_run_a_restart_interrupted(tmp_path):
    from kiro_crew.kas_workflows.router import STALE_RUNNING_PAUSE_REASON

    router, chat_rt, _sink, _ = await _ledgered(tmp_path)
    chat_rt.alive = False  # the chat's Kiro Agent process died
    assert router._undriven_live("chat-1") == ["w1"]
    new_rt = FakeRuntime()
    new_rt.answers[wire.METHOD_LOAD] = _loaded("paused", STALE_RUNNING_PAUSE_REASON)
    lines = await router.reattach("chat-1", new_rt, "acp-parent")
    assert [m for m, _ in new_rt.calls] == [wire.METHOD_LOAD, wire.METHOD_RESUME]
    assert new_rt.calls[1][1]["initiator"] == "user"
    assert "resuming" in lines[0]
    assert router.run("w1")["driven"] is True
    assert router._step_runtime["step-2"]() is new_rt, "its steps' approvals now route here"


@pytest.mark.asyncio
async def test_reattach_leaves_a_deliberate_pause_paused(tmp_path):
    router, chat_rt, _sink, _ = await _ledgered(tmp_path)
    chat_rt.alive = False
    new_rt = FakeRuntime()
    new_rt.answers[wire.METHOD_LOAD] = _loaded("paused", "Paused by user")
    lines = await router.reattach("chat-1", new_rt, "acp-parent")
    assert [m for m, _ in new_rt.calls] == [wire.METHOD_LOAD]
    assert "stays paused" in lines[0]
    assert router.run("w1")["driven"] is True, "controls now go to this process"


@pytest.mark.asyncio
async def test_reattach_never_steals_a_run_still_running_elsewhere(tmp_path):
    router, chat_rt, _sink, _ = await _ledgered(tmp_path)
    chat_rt.alive = False
    new_rt = FakeRuntime()
    new_rt.answers[wire.METHOD_LOAD] = _loaded("running")
    lines = await router.reattach("chat-1", new_rt, "acp-parent")
    assert [m for m, _ in new_rt.calls] == [wire.METHOD_LOAD]
    assert "retried" in lines[0]
    assert router.run("w1")["driven"] is False


@pytest.mark.asyncio
async def test_reattach_skips_a_run_its_live_driver_still_holds(tmp_path):
    router, _chat_rt, _sink, _ = await _ledgered(tmp_path)
    other = FakeRuntime()
    assert await router.reattach("chat-1", other, "acp-parent") == []
    assert other.calls == []


@pytest.mark.asyncio
async def test_due_reconnects_are_rate_limited_and_capped(tmp_path):
    from kiro_crew.kas_workflows import router as router_mod

    now = [1000.0]
    ledger = tmp_path / "runs.json"
    router, _ = _router(ledger_path=ledger, clock=lambda: now[0])
    router, chat_rt, _sink, _ = await _started(tmp_path, router)
    chat_rt.alive = False
    assert router.due_reconnects() == ["chat-1"]
    assert router.due_reconnects() == [], "within the interval"
    for _ in range(router_mod.RECONNECT_MAX_TRIES):
        now[0] += router_mod.RECONNECT_INTERVAL_SECS + 1
        router.due_reconnects()
    now[0] += router_mod.RECONNECT_INTERVAL_SECS + 1
    assert router.due_reconnects() == [], "gives up after the try cap"
    router.reset_reconnect_tries("chat-1")
    assert router.due_reconnects() == ["chat-1"], "a manual Reconnect starts over"


@pytest.mark.asyncio
async def test_reconnect_turn_round_trips_and_is_single_use(tmp_path, monkeypatch):
    from kiro_crew.kas_workflows import router as router_mod
    from kiro_crew.kas_workflows.router import STALE_RUNNING_PAUSE_REASON

    router, chat_rt, _sink, _ = await _ledgered(tmp_path)
    monkeypatch.setattr(router_mod, "_ROUTER", router)
    chat_rt.alive = False
    message = router.prepare_reconnect("chat-1")
    assert KasWorkflowRouter.is_turn_action(message)
    assert not KasWorkflowRouter.is_turn_action("Reconnect workflow runs please")
    new_rt = FakeRuntime()
    new_rt.answers[wire.METHOD_LOAD] = _loaded("paused", STALE_RUNNING_PAUSE_REASON)
    other_chat = await launch_from_turn("chat-2", message, _Provider(new_rt))
    assert other_chat is not None and "expired" in other_chat
    reply = await launch_from_turn("chat-1", message, _Provider(new_rt))
    assert reply is not None and "resuming" in reply
    again = await launch_from_turn("chat-1", message, _Provider(new_rt))
    assert again is not None and "expired" in again


@pytest.mark.asyncio
async def test_ordinary_turn_reattaches_in_the_background(tmp_path):
    from kiro_crew.kas_workflows.router import STALE_RUNNING_PAUSE_REASON

    router, chat_rt, _sink, _ = await _ledgered(tmp_path)
    chat_rt.alive = False
    new_rt = FakeRuntime()
    new_rt.answers[wire.METHOD_LOAD] = _loaded("paused", STALE_RUNNING_PAUSE_REASON)
    router.schedule_reattach("chat-1", _Provider(new_rt))
    await async_wait_until(lambda: router.run("w1")["driven"])
    # Nothing to do on a chat without undriven runs: no request is sent.
    quiet = FakeRuntime()
    router.schedule_reattach("chat-9", _Provider(quiet))
    assert quiet.calls == []


# ── Observing a run's end (KAS's own wake turn on the parent chat) ──


def _complete(workflow_id: str = "w5", parent: str | None = "acp-chat-5", **extra) -> dict:
    params: dict = {"workflowId": workflow_id, "status": "completed", **extra}
    if parent is not None:
        params["parentSessionId"] = parent
    return params


def _observing_router(**kwargs):
    router, _ = _router(**kwargs)
    started: list[tuple[str, str]] = []

    def starter(slot_key: str, message: str) -> bool:
        started.append((slot_key, message))
        return True

    router.set_turn_starter(starter)
    rt = FakeRuntime()
    sink = router.sink_for(rt)
    router.note_chat_session("chat-5", _Provider(rt, "acp-chat-5"))
    return router, rt, sink, started


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_chat_parented_runs_end_sends_its_chat_one_observe_turn():
    router, _rt, sink, started = _observing_router()
    sink.on_notification(
        wire.NOTIFY_RUN_COMPLETE, _complete(finalState={"workflowName": "triage `x` $s @p"})
    )
    # Scheduled off the reader's frame, not run inside it.
    assert started == []
    await async_wait_until(lambda: started)
    slot_key, message = started[0]
    assert slot_key == "chat-5"
    assert message.startswith("[Workflow `triage x s p` completed]")
    token = router.observe_token(message)
    assert token is not None
    # A repeat of the same run's end sends nothing more.
    sink.on_notification(wire.NOTIFY_RUN_COMPLETE, _complete())
    await _settle()
    assert len(started) == 1
    # Single-use and bound to its chat and the session KAS wakes.
    assert router.take_observe("chat-6", token) is None
    assert router.take_observe("chat-5", token) == "acp-chat-5"
    assert router.take_observe("chat-5", token) is None


@pytest.mark.asyncio
async def test_no_observe_turn_without_a_known_chat_parent_or_a_terminal_status():
    router, _rt, sink, started = _observing_router()
    sink.on_notification(wire.NOTIFY_RUN_COMPLETE, _complete("w6", parent="acp-nobody"))
    sink.on_notification(wire.NOTIFY_RUN_COMPLETE, _complete("w7", parent=None))
    sink.on_notification(
        wire.NOTIFY_RUN_COMPLETE,
        {"workflowId": "w8", "status": "running", "parentSessionId": "acp-chat-5"},
    )
    # Other notifications of a chat-parented run never observe.
    sink.on_notification(
        wire.NOTIFY_RUN_START,
        {"workflowId": "w9", "workflowName": "x", "parentSessionId": "acp-chat-5"},
    )
    sink.on_notification(wire.NOTIFY_PAUSED, {"workflowId": "w9", "parentSessionId": "acp-chat-5"})
    await _settle()
    assert started == []


@pytest.mark.asyncio
async def test_after_a_restart_the_ledger_names_the_runs_chat_for_its_observe_turn():
    router, _rt, sink, started = _observing_router()
    router._parent_slot.clear()  # a restarted gateway: no chat turn seen yet
    router._ledger["w10"] = {"slot": "chat-9", "cwd": "", "createdAt": 1.0, "ended": False}
    sink.on_notification(wire.NOTIFY_RUN_COMPLETE, _complete("w10", parent="acp-chat-9"))
    await async_wait_until(lambda: started)
    assert started[0][0] == "chat-9"
    token = router.observe_token(started[0][1])
    assert router.take_observe("chat-9", token) == "acp-chat-9"


def _note(severity: str | None, target: str = "acp-chat-5", wid: str = "w20") -> dict:
    params = {
        "sessionId": target,
        "callerSessionId": "step-1",
        "message": "Need a decision on the schema.",
        "workflowId": wid,
        "nodeId": "iterate",
    }
    if severity is not None:
        params["severity"] = severity
    return params


def _started_run(sink, wid: str = "w20", parent: str = "acp-chat-5") -> None:
    sink.on_notification(
        wire.NOTIFY_RUN_START,
        {"workflowId": wid, "workflowName": "review loop", "parentSessionId": parent},
    )


def test_the_sink_watches_session_notes_without_claiming_them():
    router, _ = _router()
    sink = router.sink_for(FakeRuntime())
    assert sink.watches_notification(wire.NOTIFY_SESSION_NOTIFY)
    assert not sink.claims_notification(wire.NOTIFY_SESSION_NOTIFY)
    assert not sink.watches_notification(wire.NOTIFY_RUN_COMPLETE)


@pytest.mark.asyncio
async def test_a_steps_waking_note_to_the_parent_sends_its_chat_an_observe_turn():
    router, _rt, sink, started = _observing_router()
    _started_run(sink)
    sink.on_watched_notification(wire.NOTIFY_SESSION_NOTIFY, _note("warning"))
    await async_wait_until(lambda: started)
    slot_key, message = started[0]
    assert slot_key == "chat-5"
    assert message.startswith("[Workflow `review loop` waiting]")
    assert router.take_observe("chat-5", router.observe_token(message) or "") == "acp-chat-5"
    # Every waking note is its own wake, unlike a run's one end.
    sink.on_watched_notification(wire.NOTIFY_SESSION_NOTIFY, _note("error"))
    sink.on_watched_notification(wire.NOTIFY_SESSION_NOTIFY, _note(None))
    await async_wait_until(lambda: len(started) == 3)
    assert started[1][1].startswith("[Workflow `review loop` error]")
    assert started[2][1].startswith("[Workflow `review loop` update]")


@pytest.mark.asyncio
async def test_notes_that_wake_no_parent_send_nothing():
    router, _rt, sink, started = _observing_router()
    _started_run(sink)
    # success completes the step and wakes nobody.
    sink.on_watched_notification(wire.NOTIFY_SESSION_NOTIFY, _note("success"))
    # The parent's note to one of its steps wakes the step, not the chat.
    sink.on_watched_notification(wire.NOTIFY_SESSION_NOTIFY, _note("warning", target="step-2"))
    # A note outside any workflow, and one for a run no chat parents.
    sink.on_watched_notification(
        wire.NOTIFY_SESSION_NOTIFY, {"sessionId": "acp-chat-5", "severity": "warning"}
    )
    sink.on_watched_notification(
        wire.NOTIFY_SESSION_NOTIFY, _note("warning", target="acp-nobody", wid="w21")
    )
    # Some other session-routed method.
    sink.on_watched_notification(wire.NOTIFY_RUN_COMPLETE, _note("warning"))
    await _settle()
    assert started == []


@pytest.mark.asyncio
async def test_after_a_restart_a_note_reaches_the_chat_the_ledger_names():
    router, _rt, sink, started = _observing_router()
    router._parent_slot.clear()
    router._ledger["w22"] = {"slot": "chat-9", "cwd": "", "createdAt": 1.0, "ended": False}
    sink.on_watched_notification(
        wire.NOTIFY_SESSION_NOTIFY, _note("warning", target="acp-chat-9", wid="w22")
    )
    await async_wait_until(lambda: started)
    assert started[0][0] == "chat-9"
    assert router.take_observe("chat-9", router.observe_token(started[0][1]) or "") == "acp-chat-9"


@pytest.mark.asyncio
async def test_the_parent_is_read_from_final_state_when_the_frame_lacks_it():
    _router_, _rt, sink, started = _observing_router()
    sink.on_notification(
        wire.NOTIFY_RUN_COMPLETE,
        _complete("w10", parent=None, finalState={"parentSessionId": "acp-chat-5"}),
    )
    await async_wait_until(lambda: started)
    assert started[0][0] == "chat-5"


@pytest.mark.asyncio
async def test_a_declined_or_failing_starter_leaves_no_live_token():
    router, _ = _router()
    rt = FakeRuntime()
    sink = router.sink_for(rt)
    router.note_chat_session("chat-5", _Provider(rt, "acp-chat-5"))
    messages: list[str] = []

    def declining(slot_key: str, message: str) -> bool:
        messages.append(message)
        return False

    router.set_turn_starter(declining)
    sink.on_notification(wire.NOTIFY_RUN_COMPLETE, _complete("w11"))
    await async_wait_until(lambda: messages)
    assert router.take_observe("chat-5", router.observe_token(messages[0]) or "") is None

    def failing(slot_key: str, message: str) -> bool:
        messages.append(message)
        raise RuntimeError("chat gone")

    router.set_turn_starter(failing)
    sink.on_notification(wire.NOTIFY_RUN_COMPLETE, _complete("w12"))
    await async_wait_until(lambda: len(messages) == 2)
    assert router.take_observe("chat-5", router.observe_token(messages[1]) or "") is None


@pytest.mark.asyncio
async def test_without_a_starter_a_runs_end_observes_nothing():
    router, _ = _router()
    rt = FakeRuntime()
    sink = router.sink_for(rt)
    router.note_chat_session("chat-5", _Provider(rt, "acp-chat-5"))
    sink.on_notification(wire.NOTIFY_RUN_COMPLETE, _complete("w13"))
    await _settle()
    assert router._observes == {}


def test_observe_messages_are_recognised_only_in_their_own_shape():
    router, _ = _router()
    message = router.prepare_observe("chat-1", "investigate", "failed", "acp-1")
    assert message.startswith("[Workflow `investigate` failed] ")
    assert router.observe_token(message) is not None
    assert router.observe_token("hello (observe " + "a" * 32 + ")") is None
    assert router.observe_token(message + "\nand more") is None
    # Not a launch or reconnect, so the launch short-circuit never takes it.
    assert not KasWorkflowRouter.is_turn_action(message)


@pytest.mark.asyncio
async def test_observe_from_turn_admits_only_the_woken_session(monkeypatch):
    from kiro_crew.kas_workflows import router as router_mod
    from kiro_crew.kas_workflows.router import observe_from_turn

    router, _ = _router()
    monkeypatch.setattr(router_mod, "_ROUTER", router)
    rt = FakeRuntime()

    def issue() -> str:
        return router.prepare_observe("chat-1", "investigate", "completed", "acp-parent")

    # The chat's claimed session is the one KAS woke: read the engine's turn.
    assert observe_from_turn("chat-1", issue(), _Provider(rt), is_new=False) is None
    # Spent, foreign or malformed tokens read nothing.
    spent = issue()
    assert observe_from_turn("chat-1", spent, _Provider(rt), is_new=False) is None
    assert "expired" in (observe_from_turn("chat-1", spent, _Provider(rt), is_new=False) or "")
    assert "expired" in (observe_from_turn("chat-2", issue(), _Provider(rt), is_new=False) or "")
    # A session started afresh for this turn, or another session, holds no wake.
    assert "no longer" in (observe_from_turn("chat-1", issue(), _Provider(rt), is_new=True) or "")
    moved = observe_from_turn("chat-1", issue(), _Provider(rt, "acp-other"), is_new=False)
    assert moved is not None and "no longer" in moved
    # A chat not on Kiro Agent has nothing to observe.
    other = FakeRuntime()
    other.acp_backend = "kiro"
    off = observe_from_turn("chat-1", issue(), _Provider(other), is_new=False)
    assert off is not None and "not running on Kiro Agent" in off


def test_install_turn_starter_reaches_a_router_created_later(monkeypatch):
    from kiro_crew.kas_workflows import router as router_mod

    monkeypatch.setattr(router_mod, "_ROUTER", None)
    monkeypatch.setattr(router_mod, "_TURN_STARTER", None)

    def starter(_slot: str, _message: str) -> bool:
        return True

    router_mod.install_turn_starter(starter)
    monkeypatch.setattr(router_mod, "_spawn_query_runtime", lambda: None)
    created = router_mod.get_router()
    assert created._turn_starter is starter
    router_mod.install_turn_starter(None)
    assert created._turn_starter is None


# ── Replying to a parked step ──


async def _parked(tmp_path):
    router, chat_rt, sink, _ = await _started(tmp_path)
    sink.on_notification(
        wire.NOTIFY_NODE_PAUSED,
        {
            "workflowId": "w1",
            "nodeId": "a",
            "reason": "Step requested user input via send_message.",
            "attention": [{"kind": "question", "nodeId": "a", "sessionId": "step-1"}],
        },
    )
    return router, chat_rt, sink


@pytest.mark.asyncio
async def test_a_reply_to_a_parked_step_is_a_prompt_on_its_own_session(tmp_path):
    router, chat_rt, _sink = await _parked(tmp_path)
    await router.reply_to_step("w1", "a", None, "  Use the v2 schema.  ")
    method, params = chat_rt.calls[-1]
    assert method == wire.METHOD_SESSION_PROMPT
    assert params == {
        "sessionId": "step-1",
        "prompt": [{"type": "text", "text": "Use the v2 schema."}],
    }


@pytest.mark.asyncio
async def test_a_reply_is_refused_unless_the_step_waits_for_one(tmp_path):
    router, chat_rt, _sink, _ = await _started(tmp_path)  # step "a" is running
    with pytest.raises(KasWorkflowError, match="not waiting"):
        await router.reply_to_step("w1", "a", None, "hello")
    router, chat_rt, _sink = await _parked(tmp_path)
    with pytest.raises(KasWorkflowError, match="Write a reply"):
        await router.reply_to_step("w1", "a", None, "   ")
    with pytest.raises(KasWorkflowError, match="too long"):
        await router.reply_to_step("w1", "a", None, "x" * (wire.MAX_STEP_REPLY_CHARS + 1))
    with pytest.raises(StepNotStarted):
        await router.reply_to_step("w1", "b", None, "hello")
    chat_rt.alive = False
    with pytest.raises(KasWorkflowError, match="Open the run's chat"):
        await router.reply_to_step("w1", "a", None, "hello")
    assert not any(m == wire.METHOD_SESSION_PROMPT for m, _ in chat_rt.calls)


@pytest.mark.asyncio
async def test_a_step_session_the_process_lost_is_loaded_then_prompted(tmp_path):
    router, chat_rt, _sink = await _parked(tmp_path)
    tries = {"n": 0}

    def prompt(_params):
        tries["n"] += 1
        if tries["n"] == 1:
            raise RuntimeError("Session not found")
        return {"stopReason": "end_turn"}

    async def flaky(method, params, *, timeout=30.0):
        chat_rt.calls.append((method, params))
        if method == wire.METHOD_SESSION_PROMPT:
            return prompt(params)
        return {}

    chat_rt.extension_request = flaky
    await router.reply_to_step("w1", "a", None, "go on")
    assert [m for m, _ in chat_rt.calls[-3:]] == [
        wire.METHOD_SESSION_PROMPT,
        wire.METHOD_SESSION_LOAD,
        wire.METHOD_SESSION_PROMPT,
    ]
    load = chat_rt.calls[-2][1]
    assert load["sessionId"] == "step-1" and load["cwd"] == str(tmp_path)


# ── Router edge cases ──


@pytest.mark.asyncio
async def test_a_deny_rule_refusal_leaves_an_audit_record(tmp_path):
    audited: list[dict] = []
    router, _ = _router(
        screen=lambda cmd: "denied" if "rm -rf" in cmd else None,
        audit=lambda **f: audited.append(f),
    )
    router, chat_rt, sink, _ = await _started(tmp_path, router)
    sink.on_step_request(12, "step-1", PERM, _perm("rm -rf ~/work"))
    await async_wait_until(lambda: chat_rt.responses)
    assert chat_rt.responses == [(12, {"outcome": {"outcome": "selected", "optionId": "no"}})]
    [record] = audited
    assert record["outcome"] == "auto_rejected"
    assert record["request_id"] == "12"
    assert record["metadata"]["workflowId"] == "w1"
    assert record["metadata"]["reason"].startswith("deny rule")


@pytest.mark.asyncio
async def test_a_refusal_still_goes_out_when_its_audit_fails(tmp_path):
    def broken(**_f):
        raise OSError("disk full")

    router, _ = _router(screen=lambda _c: "denied", audit=broken)
    router, chat_rt, sink, _ = await _started(tmp_path, router)
    sink.on_step_request(13, "step-1", PERM, _perm("rm -rf ~/work"))
    await async_wait_until(lambda: chat_rt.responses)
    assert chat_rt.responses == [(13, {"outcome": {"outcome": "selected", "optionId": "no"}})]


@pytest.mark.asyncio
async def test_an_auto_approval_audit_is_written_critically(tmp_path):
    # Audit-or-ask holds only when the SEL write raises on failure, which only a
    # critical write does: a plain one queues and swallows the error.
    router, chat_rt, sink, audited = await _moded_start(tmp_path, "trust_reads")
    sink.on_step_request(14, "step-1", PERM, _perm("ls"))
    await async_wait_until(lambda: chat_rt.responses)
    assert [a["outcome"] for a in audited] == ["auto_approved"]
    assert audited[0]["critical"] is True


@pytest.mark.asyncio
async def test_a_second_concurrent_reply_to_one_step_is_refused(tmp_path):
    router, chat_rt, _sink = await _parked(tmp_path)
    gate = asyncio.Event()
    prompts: list[dict] = []

    async def held_prompt(params):
        prompts.append(params)
        await gate.wait()
        return {"stopReason": "end_turn"}

    original = chat_rt.extension_request

    async def request(method, params, *, timeout=30.0):
        if method == wire.METHOD_SESSION_PROMPT:
            return await held_prompt(params)
        return await original(method, params, timeout=timeout)

    chat_rt.extension_request = request
    first = asyncio.ensure_future(router.reply_to_step("w1", "a", None, "one"))
    await async_wait_until(lambda: prompts)
    with pytest.raises(KasWorkflowError, match="already being sent"):
        # Bounded: without the claim the second reply would wait on the gate too.
        await asyncio.wait_for(router.reply_to_step("w1", "a", None, "two"), 30)
    gate.set()
    await first
    assert [p["prompt"][0]["text"] for p in prompts] == ["one"]
    # The claim is released: a later reply is judged on the step's state again.
    assert router._replies_in_flight == set()


@pytest.mark.asyncio
async def test_two_ledger_saves_never_land_in_reverse_order(tmp_path, monkeypatch):
    import threading

    import kiro_crew.atomic_write as atomic_write_mod

    router, _chat_rt, _sink, ledger = await _ledgered(tmp_path)
    real = atomic_write_mod.atomic_write
    gate = threading.Event()
    writes: list[str] = []

    def held_write(path, payload, mode=0o600):
        writes.append(payload)
        if len(writes) == 1:
            gate.wait(10)
        real(path, payload, mode=mode)

    monkeypatch.setattr(atomic_write_mod, "atomic_write", held_write)
    first = asyncio.ensure_future(router._save_ledger())
    await async_wait_until(lambda: len(writes) == 1)
    router._ledger["w2"] = {**router._ledger["w1"], "createdAt": "2026-10-19T00:00:00Z"}
    second = asyncio.ensure_future(router._save_ledger())
    lock = getattr(router, "_ledger_save_lock", None)
    # Release the first write once the second has either written (no ordering)
    # or is queued behind it (ordered) -- whichever the code under test does.
    await async_wait_until(
        lambda: len(writes) >= 2 or bool(lock is not None and getattr(lock, "_waiters", None))
    )
    gate.set()
    await asyncio.gather(first, second)
    assert '"w2"' in ledger.read_text(), "the newest ledger state is what is on disk"
