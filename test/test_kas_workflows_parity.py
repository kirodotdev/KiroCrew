"""KAS workflow parity: step/run controls, pause reasons, actions, questions, plans.

Each control is pinned to the KAS verb it calls (kiro-agent
``workflow/workflow-handlers.ts``):

- grant    -> ``_kiro/workflow/resume`` + ``extendRepeat``
- complete -> ``_kiro/workflow/update`` ``update_status`` + ``nodeId``
- retry    -> ``_kiro/workflow/retry``
- restart  -> ``_kiro/workflow/invoke`` on a finished run
- cancel   -> ``session/cancel`` on the step's own session
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from kas_workflow_helpers import (
    LOOP_PLAN,
    ChatProvider,
    FakeRuntime,
    driven,
    failed_state,
    loop_state,
    make_router,
    parallel_state,
    question_request,
    step_node,
)

from kiro_crew.kas_workflows import asks, wire
from kiro_crew.kas_workflows.mirror import RunMirror
from kiro_crew.kas_workflows.router import KasWorkflowError, KasWorkflowRouter, StepNotStarted

PERM = "session/request_permission"


# ── wire: request builders ──


def test_extend_repeat_params_is_a_user_resume_with_the_grant():
    params = wire.extend_repeat_params("w1", "loop", 3, "  one more  ")
    assert params == {
        "workflowId": "w1",
        "initiator": "user",
        "reason": "one more",
        "extendRepeat": {"nodeId": "loop", "additionalIterations": 3},
    }
    for bad in (0, -1, True, 1.5, wire.MAX_REPEAT_ITERATIONS + 1):
        with pytest.raises(ValueError):
            wire.extend_repeat_params("w1", "loop", bad)  # type: ignore[arg-type]


def test_end_step_params_targets_the_node_with_update_status():
    assert wire.end_step_params("w1", "code", "failed", "broken") == {
        "workflowId": "w1",
        "action": "update_status",
        "status": "failed",
        "nodeId": "code",
        "initiator": "user",
        "statusReason": "broken",
    }
    with pytest.raises(ValueError):
        wire.end_step_params("w1", "code", "paused")


def test_retry_params_names_one_node_or_all():
    assert wire.retry_params("w1") == {"workflowId": "w1"}
    assert wire.retry_params("w1", "code") == {"workflowId": "w1", "nodeId": "code"}


# ── wire: pause reasons and actions ──


def _pause(**kw) -> dict | None:
    """``classify_pause`` for a paused run, with every input defaulted to "nothing"."""
    base: dict[str, Any] = dict(
        pause=None, detail_class=None, pause_reason=None, user_stopped=False, attention=()
    )
    base.update(kw)
    return wire.classify_pause("paused", **base)


def test_classify_pause_names_each_kind_of_park():
    assert (
        wire.classify_pause(
            "running",
            pause=None,
            detail_class=None,
            pause_reason=None,
            user_stopped=False,
            attention=(),
        )
        is None
    )
    loop = _pause(pause=wire.PauseInfo("maxIterations", "loop", 4, 4))
    assert loop == {
        "reason": "loop_exhausted",
        "message": None,
        "nodeId": "loop",
        "iterations": {"done": 4, "max": 4},
    }
    assert _pause(detail_class="transient-error")["reason"] == "transient_error"
    assert _pause(detail_class="continuation-exhausted")["reason"] == "retries_exhausted"
    ask = wire.Attention(kind="question", node_id="code", session_id="s1")
    assert _pause(attention=(ask,)) == {
        "reason": "awaiting_input",
        "message": None,
        "nodeId": "code",
    }
    assert _pause(plan_waiting=True)["reason"] == "plan_approval"
    assert _pause(user_stopped=True)["reason"] == "user"
    assert _pause(pause_reason=wire.STALE_RUNNING_PAUSE_REASON)["reason"] == "transient_error"
    assert _pause(pause_reason="something else") == {
        "reason": "unknown",
        "message": "something else",
    }


def test_run_actions_follow_what_kas_accepts():
    def acts(status, **kw):
        base = dict(pause_pending=False, loop_exhausted=False, has_failed_nodes=False)
        base.update(kw)
        return wire.run_actions(status, **base)

    assert acts("running") == ["pause", "cancel"]
    assert acts("running", pause_pending=True) == ["cancel"]
    assert acts("paused") == ["resume", "cancel"]
    assert acts("paused", loop_exhausted=True) == ["cancel"], "a plain resume re-parks at once"
    assert acts("completed") == ["restart", "delete"]
    assert acts("failed", has_failed_nodes=True) == ["retry", "restart", "delete"]
    assert acts("failed") == ["restart", "delete"], "KAS retry refuses with nothing to retry"
    assert acts("aborted", has_failed_nodes=True) == ["retry", "restart", "delete"]


def test_include_marker_survives_the_plan_parse():
    plan = wire.parse_plan(
        [
            {
                "nodeId": "lint",
                "type": "sequence",
                "includedFrom": {"name": "lint-recipe", "path": "bundled://lint"},
                "steps": [{"nodeId": "lint/run", "type": "step"}],
            }
        ]
    )
    assert plan[0].included_from == ("lint-recipe", "bundled://lint")
    assert plan[0].to_payload()["include"] == {"recipe": "lint-recipe", "path": "bundled://lint"}
    assert plan[0].children[0].to_payload()["include"] is None


# ── mirror: run payload ──


def test_an_exhausted_loop_row_carries_pause_loop_and_node_actions():
    mirror = RunMirror()
    mirror.apply_load({"workflowId": "w1", "state": loop_state(), "nodePlan": LOOP_PLAN})
    row = mirror.row("w1")
    assert row is not None
    assert row["pause"]["reason"] == "loop_exhausted"
    assert row["pause"]["iterations"] == {"done": 2, "max": 2}
    assert row["actions"] == ["cancel"]
    loop = row["nodes"][0]
    assert loop["loop"] == {"done": 2, "max": 2}
    assert loop["actions"] == ["grant", "complete"]
    assert [it["path"] for it in loop["iterations"]] == ["loop/iter-0", "loop/iter-1"]
    assert loop["iterations"][0]["children"][0]["actions"] == []


def test_a_granted_budget_is_the_loop_max_once_the_park_clears():
    mirror = RunMirror()
    state = loop_state(status="running")
    state.pop("pause")
    state["root"]["children"][0]["status"] = "running"
    state["root"]["children"][0]["extendedMaxIterations"] = 5
    mirror.apply_load({"workflowId": "w1", "state": state, "nodePlan": LOOP_PLAN})
    row = mirror.row("w1")
    assert row["pause"] is None
    assert row["nodes"][0]["loop"] == {"done": 2, "max": 5}
    assert row["nodes"][0]["actions"] == []


def test_step_actions_follow_the_instance_status():
    mirror = RunMirror()
    state = {
        "status": "running",
        "workflowName": "x",
        "root": {
            "nodeId": "w1",
            "type": "parallel",
            "status": "running",
            "children": [
                step_node("a", "running", "sa"),
                step_node("b", "paused", "sb"),
                step_node("c", "pending"),
            ],
        },
    }
    plan = [
        {
            "nodeId": "p",
            "type": "parallel",
            "branches": [{"nodeId": k, "type": "step"} for k in "abc"],
        }
    ]
    mirror.apply_load({"workflowId": "w1", "state": state, "nodePlan": plan})
    nodes = {n["id"]: n for n in mirror.row("w1")["nodes"][0]["children"]}
    assert nodes["a"]["actions"] == ["cancel"]
    assert nodes["b"]["actions"] == ["complete", "fail", "reply"]
    assert nodes["c"]["actions"] == []


def test_paused_notification_and_user_stop_set_the_reason():
    mirror = RunMirror()
    mirror.apply_notification(wire.NOTIFY_RUN_START, {"workflowId": "w1", "workflowName": "x"})
    mirror.apply_notification(
        wire.NOTIFY_PAUSED,
        {
            "workflowId": "w1",
            "pauseReason": "throttled",
            "pauseDetail": {"class": "transient-error", "code": "ThrottlingException"},
        },
    )
    assert mirror.row("w1")["pause"]["reason"] == "transient_error"
    mirror.apply_notification(
        wire.NOTIFY_RUN_COMPLETE,
        {
            "workflowId": "w1",
            "status": "paused",
            "finalState": {"pauseReason": "Step 'a' stopped by user.", "stopInitiator": "user"},
        },
    )
    assert mirror.row("w1")["pause"]["reason"] == "user"
    mirror.apply_notification(wire.NOTIFY_NODE_START, {"workflowId": "w1", "nodeId": "a"})
    assert mirror.row("w1")["pause"] is None


def test_a_failed_run_offers_retry_only_with_a_failed_step():
    mirror = RunMirror()
    state = {
        "status": "failed",
        "workflowName": "x",
        "root": {
            "nodeId": "w1",
            "type": "sequence",
            "status": "failed",
            "children": [step_node("a", "failed", "sa")],
        },
    }
    mirror.apply_load(
        {"workflowId": "w1", "state": state, "nodePlan": [{"nodeId": "a", "type": "step"}]}
    )
    assert mirror.row("w1")["actions"] == ["retry", "restart", "delete"]


# ── router: controls -> KAS verbs ──


@pytest.mark.asyncio
async def test_grant_sends_resume_with_extend_repeat_on_the_driver():
    router = make_router()
    rt = driven(router, loop_state(), LOOP_PLAN)
    await router.grant("w1", "loop", 2)
    assert rt.calls[-1] == (
        wire.METHOD_RESUME,
        {
            "workflowId": "w1",
            "initiator": "user",
            "extendRepeat": {"nodeId": "loop", "additionalIterations": 2},
        },
    )
    with pytest.raises(KasWorkflowError):
        await router.grant("w1", "loop", 0)


@pytest.mark.asyncio
async def test_a_kas_refusal_of_a_grant_is_a_workflow_error():
    router = make_router()
    rt = driven(router, loop_state(), LOOP_PLAN)
    rt.answers[wire.METHOD_RESUME] = RuntimeError("above the 1000 cap")
    with pytest.raises(KasWorkflowError, match="1000 cap"):
        await router.grant("w1", "loop", 999)


@pytest.mark.asyncio
async def test_controls_need_the_process_driving_the_run():
    router = make_router()
    router.mirror.apply_load({"workflowId": "w1", "state": loop_state(), "nodePlan": LOOP_PLAN})
    with pytest.raises(KasWorkflowError, match="not connected"):
        await router.grant("w1", "loop", 1)
    with pytest.raises(KasWorkflowError, match="not connected"):
        await router.end_step("w1", "loop", "completed")
    row = router.run_row("w1")
    assert row["actions"] == [] and row["nodes"][0]["actions"] == []


@pytest.mark.asyncio
async def test_end_step_sends_update_status_and_raises_kas_refusals():
    router = make_router()
    rt = driven(router, loop_state(), LOOP_PLAN)
    rt.answers[wire.METHOD_UPDATE] = {"updated": True, "message": "Repeat 'loop' completed."}
    assert await router.end_step("w1", "loop", "completed", "good enough") == (
        "Repeat 'loop' completed."
    )
    assert rt.calls[-1] == (
        wire.METHOD_UPDATE,
        {
            "workflowId": "w1",
            "action": "update_status",
            "status": "completed",
            "nodeId": "loop",
            "initiator": "user",
            "statusReason": "good enough",
        },
    )
    rt.answers[wire.METHOD_UPDATE] = {
        "updated": False,
        "message": "Cannot update node 'loop': nodeId targeting supports status 'completed' only.",
    }
    with pytest.raises(KasWorkflowError, match="completed' only"):
        await router.end_step("w1", "loop", "failed")


@pytest.mark.asyncio
async def test_retry_sends_retry_and_marks_the_run_live_again(tmp_path):
    router = make_router(ledger_path=tmp_path / "runs.json")
    rt = driven(router, failed_state())
    # driven()'s notification spawns the router's first-sight inspect of the run;
    # let it land now, or under load it lands after the retry and is the last call.
    await asyncio.gather(*router._tasks)
    await router.ensure_ledger()
    router._ledger["w1"] = {"slot": "chat-1", "cwd": "", "createdAt": 1.0, "ended": True}
    rt.answers[wire.METHOD_RETRY] = {
        "workflowId": "w1",
        "status": "running",
        "retriedNodeIds": ["code"],
    }
    assert await router.retry("w1") == ["code"]
    assert rt.calls[-1] == (wire.METHOD_RETRY, {"workflowId": "w1"})
    assert router._ledger["w1"]["ended"] is False
    with pytest.raises(KasWorkflowError, match="inside a loop"):
        await router.retry("w1", "code", "loop/iter-0/code")
    with pytest.raises(KasWorkflowError, match="failed or stopped step"):
        await router.retry("w1", "a")


@pytest.mark.asyncio
async def test_retry_and_restart_refuse_a_live_run():
    router = make_router()
    driven(router, loop_state(), LOOP_PLAN)
    with pytest.raises(KasWorkflowError, match="retried"):
        await router.retry("w1")
    with pytest.raises(KasWorkflowError, match="finished run"):
        await router.restart("w1")


@pytest.mark.asyncio
async def test_restart_invokes_a_finished_run_on_its_chats_process():
    router = make_router()
    router.mirror.apply_load({"workflowId": "w1", "state": failed_state()})
    router._ledger["w1"] = {"slot": "chat-1", "cwd": "", "createdAt": 1.0, "ended": True}
    with pytest.raises(KasWorkflowError, match="not connected"):
        await router.restart("w1")
    chat_rt = FakeRuntime()
    router.note_chat_session("chat-1", ChatProvider(chat_rt, "acp-parent"))
    assert "restart" in router.run_row("w1")["actions"]
    await router.restart("w1")
    assert chat_rt.calls[-1] == (wire.METHOD_INVOKE, {"workflowId": "w1"})
    assert router.run_row("w1")["driven"] is True


@pytest.mark.asyncio
async def test_a_chat_on_another_session_does_not_redrive_the_run():
    router = make_router()
    router.mirror.apply_load({"workflowId": "w1", "state": failed_state()})
    router._ledger["w1"] = {"slot": "chat-1", "cwd": "", "createdAt": 1.0, "ended": True}
    router.note_chat_session("chat-1", ChatProvider(FakeRuntime(), "acp-newer"))
    with pytest.raises(KasWorkflowError, match="not connected"):
        await router.restart("w1")
    assert router.run_row("w1")["actions"] == ["delete"]


@pytest.mark.asyncio
async def test_cancel_step_sends_session_cancel_and_withdraws_its_asks():
    router = make_router()
    rt = driven(router, parallel_state())
    sink = router.sink_for(rt)
    router._step_runtime["sa"] = router._run_runtime["w1"]
    sink.on_step_request(
        7,
        "sa",
        PERM,
        {
            "sessionId": "sa",
            "toolCall": {"title": "Run ls", "rawInput": {"command": "ls"}},
            "options": [
                {"optionId": "y", "kind": "allow_once", "name": "Allow"},
                {"optionId": "n", "kind": "reject_once", "name": "Deny"},
            ],
        },
    )
    from kiro_crew.testing.wait import async_wait_until

    await async_wait_until(lambda: router.approvals(), timeout=2.0)
    await router.cancel_step("w1", "a")
    assert rt.notifications == [(wire.METHOD_SESSION_CANCEL, {"sessionId": "sa"})]
    assert rt.responses == [(7, {"outcome": {"outcome": "cancelled"}})]
    assert router.approvals() == []


@pytest.mark.asyncio
async def test_cancel_step_needs_a_running_step():
    router = make_router()
    driven(router, parallel_state())
    with pytest.raises(StepNotStarted):
        await router.cancel_step("w1", "nope")
    router.mirror.apply_notification(
        wire.NOTIFY_NODE_COMPLETE, {"workflowId": "w1", "nodeId": "b", "status": "completed"}
    )
    with pytest.raises(KasWorkflowError, match="not running"):
        await router.cancel_step("w1", "b")


# ── router: questions ──


def test_a_question_is_told_apart_from_an_approval():
    assert asks.is_question_request(question_request())
    approval = {
        "options": [
            {"optionId": "y", "kind": "allow_once"},
            {"optionId": "n", "kind": "reject_once"},
        ]
    }
    assert not asks.is_question_request(approval)
    assert not asks.is_question_request({"options": []})


async def _held_question(router: KasWorkflowRouter, mode: str = "trust"):
    from kiro_crew.testing.wait import async_wait_until

    rt = driven(router, parallel_state())
    router._ledger["w1"] = {"slot": "chat-1", "cwd": "", "createdAt": 1.0, "ended": False}
    router.note_chat_session("chat-1", ChatProvider(rt), mode=lambda: (mode, mode))
    router._step_runtime["sa"] = router._run_runtime["w1"]
    router.sink_for(rt).on_step_request(11, "sa", PERM, question_request())
    await async_wait_until(lambda: router.questions(), timeout=2.0)
    return rt


@pytest.mark.asyncio
async def test_a_question_waits_for_a_person_even_under_trust():
    router = make_router(audit=lambda **_k: None)
    rt = await _held_question(router, mode="trust")
    await router.recheck_pending()
    assert rt.responses == [], "Trust never picks an answer"
    assert router.approvals() == []
    (held,) = router.questions()
    assert held["workflowId"] == "w1" and held["nodeId"] == "a" and held["slot"] == "chat-1"
    assert held["path"] == "a"
    assert held["questions"] == [
        {
            "id": "tc-9",
            "prompt": "Which database?",
            "options": [{"label": "Postgres"}, {"label": "MySQL"}],
            "multiSelect": False,
            "freeText": False,
        }
    ]


@pytest.mark.asyncio
async def test_answering_a_question_picks_the_option_by_its_label():
    router = make_router(audit=lambda **_k: None)
    rt = await _held_question(router)
    key = router.questions()[0]["key"]
    with pytest.raises(KasWorkflowError, match="not one of the offered"):
        await router.answer_question(key, {"tc-9": "Oracle"})
    with pytest.raises(KasWorkflowError, match="one answer"):
        await router.answer_question(key, {"tc-9": ["Postgres", "MySQL"]})
    await router.answer_question(key, {"tc-9": ["MySQL"]})
    assert rt.responses == [(11, {"outcome": {"outcome": "selected", "optionId": "my"}})]
    assert router.questions() == []
    with pytest.raises(KasWorkflowError, match="no longer waiting"):
        await router.answer_question(key, {"tc-9": "MySQL"})


@pytest.mark.asyncio
async def test_dismissing_a_question_answers_cancelled():
    router = make_router(audit=lambda **_k: None)
    rt = await _held_question(router)
    await router.answer_question(router.questions()[0]["key"], None, dismiss=True)
    assert rt.responses == [(11, {"outcome": {"outcome": "cancelled"}})]


# ── router: plans ──


@pytest.mark.asyncio
async def test_a_plan_approval_carries_its_text_and_step_tree():
    from kiro_crew.testing.wait import async_wait_until

    router = make_router()
    rt = driven(router, parallel_state())
    router._step_runtime["sa"] = router._run_runtime["w1"]
    params = {
        "sessionId": "sa",
        "toolCall": {
            "title": "Approve the plan",
            "content": [
                {"type": "content", "content": {"type": "text", "text": "1. code\n2. review"}}
            ],
        },
        "options": [
            {"optionId": "y", "kind": "allow_once", "name": "Allow"},
            {"optionId": "n", "kind": "reject_once", "name": "Deny"},
        ],
        "_meta": {
            "kiro": {
                "toolId": "update_workflow",
                "workflowPlan": {
                    "workflowId": "w1",
                    "kind": "launch",
                    "workflowRef": "generated://gen_1",
                    "keptStepIds": ["create-workflow"],
                    "steps": [
                        {
                            "nodeId": "loop",
                            "type": "repeat",
                            "maxIterations": 3,
                            "steps": [
                                {
                                    "nodeId": "code\u202e",
                                    "type": "step",
                                    "agentName": "coder",
                                    "modelId": "auto",
                                    "task": {"text": "Write it", "truncated": True},
                                }
                            ],
                        }
                    ],
                },
            }
        },
    }
    router.sink_for(rt).on_step_request(3, "sa", PERM, params)
    await async_wait_until(lambda: router.approvals(), timeout=2.0)
    (approval,) = router.approvals()
    plan = approval["plan"]
    assert approval["isPlan"] is True
    assert plan["text"] == "1. code\n2. review"
    assert plan["kind"] == "launch" and plan["keptStepIds"] == ["create-workflow"]
    loop = plan["nodes"][0]
    assert (loop["id"], loop["type"], loop["maxIterations"]) == ("loop", "repeat", 3)
    code = loop["children"][0]
    assert code["id"] == "code ", "a bidi override is neutralized"
    assert code["model"] is None and code["agent"] == "coder"
    assert code["task"] == {"text": "Write it", "truncated": True}


def test_a_plan_without_its_tree_keeps_only_the_text():
    assert asks.plan_payload("the plan", None) == {"text": "the plan"}
    assert asks.plan_payload("the plan", {"steps": [], "omitted": "steps"}) == {
        "text": "the plan",
        "omitted": "steps",
    }


@pytest.mark.asyncio
async def test_an_open_plan_approval_is_asked_again_after_a_restart(tmp_path):
    """A kept plan wait: load the step session (KAS re-asks), never resume the run."""
    router = make_router(ledger_path=tmp_path / "runs.json")
    await router.ensure_ledger()
    router._ledger["w1"] = {
        "slot": "chat-1",
        "cwd": str(tmp_path),
        "createdAt": 1.0,
        "ended": False,
    }
    chat_rt = FakeRuntime()
    router.sink_for(chat_rt)
    state = {
        "status": "paused",
        "workflowName": "workflow-from-prompt",
        "pauseReason": wire.STALE_RUNNING_PAUSE_REASON,
        "attention": [
            {
                "kind": "permission",
                "nodeId": "create-workflow",
                "nodePath": ["w1", "create-workflow"],
                "sessionId": "creator-1",
                "toolCallId": "tc-plan",
                "since": "2026-10-17T00:00:00Z",
            }
        ],
        "root": {
            "nodeId": "w1",
            "type": "sequence",
            "status": "paused",
            "children": [step_node("create-workflow", "paused", "creator-1")],
        },
    }
    chat_rt.answers[wire.METHOD_LOAD] = {
        "workflowId": "w1",
        "state": state,
        "stepSessions": [
            {
                "nodeId": "create-workflow",
                "nodePath": ["w1", "create-workflow"],
                "sessionId": "creator-1",
            }
        ],
    }
    lines = await router.reattach("chat-1", chat_rt, "acp-parent")
    methods = [m for m, _ in chat_rt.calls]
    assert wire.METHOD_RESUME not in methods, "resuming would cancel the kept plan"
    assert chat_rt.calls[-1] == (
        wire.METHOD_SESSION_LOAD,
        {"sessionId": "creator-1", "cwd": str(tmp_path), "mcpServers": []},
    )
    assert "plan approval again" in lines[0]
    # The re-asked request now reaches this router, not the runtime's auto-reject.
    sink = router.sink_for(chat_rt)
    assert sink.claims_step_request("creator-1", PERM)
    assert router.run_row("w1")["pause"]["reason"] == "plan_approval"
    # No plan card is held yet (the re-ask has not arrived, or failed): Resume
    # stays, or the run would have no way forward.
    assert "resume" in router.run_row("w1")["actions"]

    # The re-ask arrives and the router holds its card: Resume would reject
    # the plan the card asks about (KAS answers a re-asked plan ``cancelled``
    # once its step moves on), so it is hidden while the card is open.
    from kiro_crew.testing.wait import async_wait_until

    sink.on_step_request(
        5,
        "creator-1",
        PERM,
        {
            "sessionId": "creator-1",
            "toolCall": {"toolCallId": "tc-plan", "title": "Approve the plan"},
            "options": [
                {"optionId": "y", "kind": "allow_once", "name": "Allow"},
                {"optionId": "n", "kind": "reject_once", "name": "Deny"},
            ],
            "_meta": {
                "kiro": {
                    "toolId": "update_workflow",
                    "workflowPlan": {"workflowId": "w1", "kind": "launch", "steps": []},
                }
            },
        },
    )
    await async_wait_until(lambda: router.approvals(), timeout=2.0)
    row = router.run_row("w1")
    assert row["pause"]["reason"] == "plan_approval"
    assert row["actions"] == ["cancel"], "no Resume beside a held plan card"
    assert [r["actions"] for r in router.rows() if r["workflowId"] == "w1"] == [["cancel"]]


def test_resume_is_hidden_only_while_a_plan_card_is_held():
    def acts(**kw):
        base = dict(pause_pending=False, loop_exhausted=False, has_failed_nodes=False)
        base.update(kw)
        return wire.run_actions("paused", **base)

    assert acts(plan_card_held=True) == ["cancel"]
    assert acts(plan_card_held=False) == ["resume", "cancel"]
    # Only a paused run's Resume is affected.
    assert wire.run_actions(
        "running",
        pause_pending=False,
        loop_exhausted=False,
        has_failed_nodes=False,
        plan_card_held=True,
    ) == ["pause", "cancel"]


@pytest.mark.asyncio
async def test_a_stale_run_without_a_kept_plan_still_resumes(tmp_path):
    router = make_router(ledger_path=tmp_path / "runs.json")
    await router.ensure_ledger()
    router._ledger["w1"] = {
        "slot": "chat-1",
        "cwd": str(tmp_path),
        "createdAt": 1.0,
        "ended": False,
    }
    chat_rt = FakeRuntime()
    state = {
        "status": "paused",
        "workflowName": "x",
        "pauseReason": wire.STALE_RUNNING_PAUSE_REASON,
        "root": {
            "nodeId": "w1",
            "type": "sequence",
            "status": "paused",
            "children": [step_node("a", "paused", "sa")],
        },
    }
    chat_rt.answers[wire.METHOD_LOAD] = {"workflowId": "w1", "state": state}
    await router.reattach("chat-1", chat_rt, "acp-parent")
    assert [m for m, _ in chat_rt.calls] == [wire.METHOD_LOAD, wire.METHOD_RESUME]


# ── router: launch options ──


@pytest.mark.asyncio
async def test_launch_options_reach_workflow_new(tmp_path):
    router = make_router()
    chat_rt = FakeRuntime()
    chat_rt.answers[wire.METHOD_NEW] = {"workflowId": "w5", "initialState": {"status": "running"}}
    launch = router.prepare_launch(
        "chat-1", str(tmp_path), "bundled://x", {}, model="claude-x", effort="high"
    )
    await router.start_in_session(chat_rt, "acp-parent", launch)
    params = chat_rt.calls[0][1]
    assert (params["modelId"], params["effortLevel"]) == ("claude-x", "high")
    bare = router.prepare_launch("chat-1", str(tmp_path), "bundled://x", {})
    await router.start_in_session(chat_rt, "acp-parent", bare)
    assert "modelId" not in chat_rt.calls[2][1] and "effortLevel" not in chat_rt.calls[2][1]
