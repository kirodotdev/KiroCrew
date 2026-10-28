"""Backend run state: one test group per behaviour the run mirror must match."""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew.kas_workflows import wire
from kiro_crew.kas_workflows.mirror import RunMirror
from kiro_crew.kas_workflows.router import DashboardHooks, KasWorkflowError, KasWorkflowRouter
from kiro_crew.messaging.workflow_markers import event_line, format_duration
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

_TREE = [
    {"type": "step", "id": "plan", "agent": "wf-planner"},
    {
        "type": "repeat",
        "id": "loop",
        "maxIterations": 3,
        "children": [{"type": "step", "id": "code", "agent": "wf-coder"}],
    },
    {"type": "watch", "id": "ci", "label": "CI"},
    {"type": "step", "id": "ship", "agent": "wf-shipper"},
]

_NOW = 1_800_000_000.0


def _clock() -> float:
    return _NOW


def _mirror(parent: str = "chat-1") -> RunMirror:
    mirror = RunMirror(clock=_clock)
    mirror.apply_notification(
        wire.NOTIFY_RUN_START,
        {
            "workflowId": "wf_1",
            "workflowName": "review-loop",
            "nodeTree": _TREE,
            "parentSessionId": parent,
        },
    )
    return mirror


def _node(row: dict, node_id: str) -> dict:
    def walk(nodes: list) -> dict | None:
        for node in nodes:
            if node["id"] == node_id:
                return node
            found = walk(node["children"])
            if found:
                return found
        return None

    found = walk(row["nodes"])
    assert found is not None, node_id
    return found


def _start(mirror: RunMirror, node_id: str, session: str, **extra: Any) -> None:
    mirror.apply_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "wf_1", "nodeId": node_id, "sessionId": session, **extra},
    )


# ── Gap 1: retry-wait backoff keeps the step running ──


def test_a_retry_wait_keeps_the_step_running_with_its_backoff_line() -> None:
    mirror = _mirror()
    _start(mirror, "plan", "s-plan", nodePath=["wf_1", "plan"])
    mirror.apply_notification(
        wire.NOTIFY_NODE_PAUSED,
        {
            "workflowId": "wf_1",
            "nodeId": "plan",
            "nodePath": ["wf_1", "plan"],
            "reason": "Rate limited; retrying in 30s (attempt 2 of 5)",
            "kind": "retry-wait",
        },
    )
    row = mirror.row("wf_1")
    assert row is not None
    plan = _node(row, "plan")
    assert plan["status"] == "running"
    assert plan["retryWait"] == {
        "reason": "Rate limited; retrying in 30s (attempt 2 of 5)",
        "since": "2027-01-15T08:00:00Z",
    }
    # A live step: Stop only, never Complete / Fail / Reply.
    assert plan["actions"] == ["cancel"]
    assert row["controls"]["needsAttention"] is False
    assert mirror.step_awaits_reply("wf_1", "s-plan") is False
    # The re-emitted node_start ends the wait.
    _start(mirror, "plan", "s-plan", nodePath=["wf_1", "plan"])
    assert _node(mirror.row("wf_1"), "plan")["retryWait"] is None  # type: ignore[arg-type]


def test_a_genuine_node_park_still_pauses_the_step() -> None:
    mirror = _mirror()
    _start(mirror, "plan", "s-plan")
    mirror.apply_notification(
        wire.NOTIFY_NODE_PAUSED,
        {"workflowId": "wf_1", "nodeId": "plan", "reason": "waiting", "kind": "paused"},
    )
    plan = _node(mirror.row("wf_1"), "plan")  # type: ignore[arg-type]
    assert plan["status"] == "paused" and plan["retryWait"] is None
    assert plan["actions"] == ["complete", "fail", "reply"]


# ── Gap 2: a stale snapshot never revives a finished run ──


def test_a_stale_list_after_the_end_keeps_the_run_finished() -> None:
    mirror = _mirror()
    mirror.apply_notification(
        wire.NOTIFY_RUN_COMPLETE, {"workflowId": "wf_1", "status": "completed", "finalState": {}}
    )
    stale = wire.parse_run_summary({"workflowId": "wf_1", "status": "running", "name": "r"})
    assert stale is not None
    mirror.apply_summaries([stale])
    assert mirror.status("wf_1") == "completed"
    # A stale inspect answer too, node statuses included.
    mirror.apply_load(
        {
            "workflowId": "wf_1",
            "state": {
                "status": "running",
                "root": {"id": "root", "children": [{"id": "plan", "status": "running"}]},
            },
        }
    )
    row = mirror.row("wf_1")
    assert row is not None and row["status"] == "completed"
    assert _node(row, "plan")["status"] == "pending"
    # Only a lifecycle event brings it back (KAS sends run_start on resume/retry).
    mirror.apply_notification(wire.NOTIFY_RUN_START, {"workflowId": "wf_1"})
    assert mirror.status("wf_1") == "running"


# ── Gap 7: "needs you" and Pause on a running run with an approval ──


def test_an_approval_wait_keeps_a_running_run_running_with_pause() -> None:
    mirror = _mirror()
    _start(
        mirror,
        "plan",
        "s-plan",
        attention=[{"kind": "permission", "nodeId": "plan", "sessionId": "s-plan"}],
    )
    controls = mirror.row("wf_1")["controls"]  # type: ignore[index]
    assert controls["display"] == "running"
    assert controls["primary"] == "pause"
    assert (controls["needsAttention"], controls["attentionKind"]) == (True, "approval")


def test_a_question_wait_reads_paused_and_opens_the_step() -> None:
    mirror = _mirror()
    _start(
        mirror,
        "plan",
        "s-plan",
        attention=[{"kind": "question", "nodeId": "plan", "sessionId": "s-plan"}],
    )
    controls = mirror.row("wf_1")["controls"]  # type: ignore[index]
    assert (controls["display"], controls["primary"]) == ("paused", "open")
    assert controls["attentionKind"] == "input"


def test_a_park_nobody_asked_for_needs_you_and_sorts_first() -> None:
    mirror = RunMirror(clock=_clock)
    for wid in ("wf_live", "wf_user", "wf_loop"):
        mirror.apply_notification(wire.NOTIFY_RUN_START, {"workflowId": wid, "nodeTree": _TREE})
    mirror.apply_notification(
        wire.NOTIFY_PAUSED,
        {"workflowId": "wf_user", "pauseReason": "Paused by user", "initiator": "user"},
    )
    mirror.apply_notification(
        wire.NOTIFY_PAUSED,
        {
            "workflowId": "wf_loop",
            "pauseReason": "Repeat 'loop' reached maxIterations.",
            "pause": {"kind": "maxIterations", "nodeId": "loop", "maxIterations": 3},
        },
    )
    rows = {r["workflowId"]: r for r in mirror.rows()}
    assert rows["wf_loop"]["controls"]["needsAttention"] is True
    assert rows["wf_loop"]["controls"]["attentionKind"] == "parked"
    assert rows["wf_user"]["controls"]["needsAttention"] is False
    assert rows["wf_live"]["controls"]["needsAttention"] is False
    assert mirror.rows()[0]["workflowId"] == "wf_loop"


@pytest.mark.parametrize(
    "status,attention,reason,kind",
    [
        ("running", (), None, None),
        ("paused", (), wire.PAUSE_USER, None),
        ("paused", (), wire.PAUSE_TRANSIENT_ERROR, "parked"),
        ("paused", (), wire.PAUSE_PLAN_APPROVAL, "approval"),
        ("completed", (wire.Attention("question", "n"),), None, None),
    ],
)
def test_attention_kind_follows_the_ides_one_rule(status, attention, reason, kind) -> None:
    assert wire.attention_kind(status, attention, reason) == kind


# ── Gap 11: plan updates and queued replacements ──


def _queued(mirror: RunMirror, **resolution: Any) -> None:
    body: dict[str, Any] = {
        "workflowId": "wf_1",
        "pendingSteps": [{"type": "step", "id": "fix", "agent": "wf-fixer"}],
    }
    if resolution:
        body["resolution"] = resolution
    mirror.apply_notification(wire.NOTIFY_STEPS_QUEUED, body)


def test_a_queued_plan_marks_the_unstarted_steps_it_replaces() -> None:
    mirror = _mirror()
    _start(mirror, "plan", "s-plan")
    _queued(mirror)
    row = mirror.row("wf_1")
    assert row is not None
    assert [n["id"] for n in row["queuedSteps"]] == ["fix"]
    assert _node(row, "plan")["replacementPending"] is False, "already started"
    assert _node(row, "ship")["replacementPending"] is True
    assert _node(row, "code")["replacementPending"] is False, "not top level"


def test_a_rejected_plan_update_is_kept_with_its_reason() -> None:
    mirror = _mirror()
    _queued(mirror)
    _queued(mirror, outcome="rejected", reason="step 'fix' names an unknown agent")
    row = mirror.row("wf_1")
    assert row is not None
    assert row["queuedSteps"] == []
    assert row["lastPlanUpdate"] == {
        "outcome": "rejected",
        "reason": "step 'fix' names an unknown agent",
        "at": "2027-01-15T08:00:00Z",
    }
    assert row["planRevision"] == 0
    assert not any(_node(row, n)["replacementPending"] for n in ("plan", "ship"))


def test_an_applied_plan_update_counts_a_revision() -> None:
    mirror = _mirror()
    _start(mirror, "plan", "s-plan")
    _queued(mirror, outcome="applied")
    row = mirror.row("wf_1")
    assert row is not None
    assert row["planRevision"] == 1
    assert row["lastPlanUpdate"]["outcome"] == "applied"
    assert [n["id"] for n in row["nodes"]] == ["plan", "fix"]


# ── Gap 19: a project's list is authoritative for membership ──


def _summary(wid: str, status: str = "completed") -> wire.RunSummary:
    summary = wire.parse_run_summary({"workflowId": wid, "status": status, "name": wid})
    assert summary is not None
    return summary


def test_a_run_a_later_list_omits_is_forgotten() -> None:
    mirror = RunMirror(clock=_clock)
    mirror.apply_scoped_list("/p", [_summary("a"), _summary("b"), _summary("c", "running")])
    mirror.apply_scoped_list("/q", [_summary("q1")])
    _changed, forgotten = mirror.apply_scoped_list(
        "/p", [_summary("a")], keep=lambda wid: wid == "c"
    )
    assert forgotten == ["b"]
    assert "b" not in mirror and "a" in mirror
    assert "c" in mirror, "a run this gateway drives is kept"
    assert "q1" in mirror, "another project's run is never touched"


# ── Gap 23: per-step time, watch outcome, waits, active instances ──


def test_steps_carry_their_start_and_end() -> None:
    mirror = _mirror()
    _start(mirror, "plan", "s-plan", startedAt="2027-01-15T07:58:00Z")
    row = mirror.row("wf_1")
    assert _node(row, "plan")["startedAt"] == "2027-01-15T07:58:00Z"  # type: ignore[arg-type]
    assert _node(row, "plan")["endedAt"] is None  # type: ignore[arg-type]
    mirror.apply_notification(
        wire.NOTIFY_NODE_COMPLETE,
        {"workflowId": "wf_1", "nodeId": "plan", "status": "completed"},
    )
    plan = _node(mirror.row("wf_1"), "plan")  # type: ignore[arg-type]
    assert (plan["startedAt"], plan["endedAt"]) == ("2027-01-15T07:58:00Z", "2027-01-15T08:00:00Z")


def test_a_watch_node_is_a_leaf_with_its_last_outcome() -> None:
    mirror = _mirror()
    mirror.apply_notification(
        wire.NOTIFY_WATCH_POLL,
        {"workflowId": "wf_1", "nodeId": "ci", "outcome": "new-activity", "at": "T1"},
    )
    ci = _node(mirror.row("wf_1"), "ci")  # type: ignore[arg-type]
    assert ci["watchOutcome"] == {"outcome": "new-activity", "at": "T1"}
    assert ci["children"] == []
    assert _node(mirror.row("wf_1"), "plan")["watchOutcome"] is None  # type: ignore[arg-type]


def test_a_node_names_what_it_waits_for() -> None:
    mirror = _mirror()
    _start(
        mirror,
        "plan",
        "s-plan",
        attention=[
            {"kind": "permission", "nodeId": "plan", "sessionId": "s-plan"},
            {"kind": "question", "nodeId": "ship", "nodePath": ["wf_1", "ship"]},
        ],
    )
    row = mirror.row("wf_1")
    assert _node(row, "plan")["waitingFor"] == "approval"  # type: ignore[arg-type]
    assert _node(row, "ship")["waitingFor"] == "input"  # type: ignore[arg-type]
    assert _node(row, "code")["waitingFor"] is None  # type: ignore[arg-type]


def test_active_steps_name_the_loop_iteration_and_end_with_the_run() -> None:
    mirror = _mirror()
    assert mirror.row("wf_1")["stepsKnown"] is True  # type: ignore[index]
    _start(mirror, "code", "s-code-1", nodePath=["wf_1", "loop", "iter-1", "code"])
    row = mirror.row("wf_1")
    assert row is not None
    assert row["activeSteps"] == [
        {"nodeId": "code", "label": "wf-coder", "sessionId": "s-code-1", "path": "loop/iter-1/code"}
    ]
    # A finished run whose final state still says a step runs has none.
    mirror.apply_notification(
        wire.NOTIFY_RUN_COMPLETE, {"workflowId": "wf_1", "status": "failed", "finalState": {}}
    )
    assert mirror.row("wf_1")["activeSteps"] == []  # type: ignore[index]


def test_a_placeholder_run_says_its_steps_are_not_known() -> None:
    mirror = RunMirror(clock=_clock)
    mirror.apply_notification(wire.NOTIFY_PAUSED, {"workflowId": "wf_x", "pauseReason": "x"})
    assert mirror.row("wf_x")["stepsKnown"] is False  # type: ignore[index]


# ── Gap 29: a run's parent is fixed once known ──


def test_a_conflicting_parent_is_ignored() -> None:
    mirror = _mirror(parent="chat-1")
    _start(mirror, "plan", "s-plan", parentSessionId="chat-2")
    assert mirror.parent_of("wf_1") == "chat-1"
    summary = wire.parse_run_summary(
        {"workflowId": "wf_1", "status": "running", "parentSessionId": "chat-3"}
    )
    assert summary is not None
    mirror.apply_summaries([summary])
    assert mirror.parent_of("wf_1") == "chat-1"


# ── Gap 20: extensionMethods gating (wire) ──


def test_extension_methods_reads_the_advertised_list_or_none() -> None:
    caps = {"_meta": {"kiro": {"extensionMethods": [wire.METHOD_PAUSE]}}}
    assert wire.extension_methods(caps) == frozenset({wire.METHOD_PAUSE})
    assert wire.extension_methods({}) is None
    assert wire.extension_methods({"_meta": {"kiro": {"extensionMethods": "x"}}}) is None


def test_gate_actions_drops_only_what_the_agent_does_not_serve() -> None:
    actions = ["retry", "restart", "delete"]
    assert wire.gate_actions(actions, None) == actions, "a legacy agent serves everything"
    served = frozenset({wire.METHOD_INVOKE, wire.METHOD_DELETE})
    assert wire.gate_actions(actions, served) == ["restart", "delete"]
    nodes = ["complete", "fail", "reply", "cancel", "grant"]
    assert wire.gate_actions(nodes, frozenset(), node=True) == ["reply", "cancel"]


# ── Gap 12: no policyPreset (Crew chats seed none; presets would ADD allow rules) ──


def test_resume_retry_and_grant_assert_no_policy_presets() -> None:
    for params in (
        wire.user_control_params("wf_1"),
        wire.retry_params("wf_1", "n"),
        wire.extend_repeat_params("wf_1", "loop", 2),
    ):
        assert "policyPreset" not in params


# ── Gap 27: recipe scope, a built-in without a source, repeat config ──


def test_a_built_in_without_a_source_launches_by_its_bundled_uri() -> None:
    recipe = wire.parse_recipe({"name": "goal", "builtIn": True})
    assert recipe is not None and recipe.source == "bundled://goal"
    payload = recipe.to_payload("/proj")
    assert (payload["launchable"], payload["scope"], payload["group"]) == (
        True,
        "bundled",
        "global",
    )


def test_a_recipe_without_a_source_is_kept_but_not_launchable() -> None:
    recipe = wire.parse_recipe({"name": "broken", "validationError": "bad"})
    assert recipe is not None
    assert recipe.to_payload()["launchable"] is False


def test_recipes_are_scoped_to_the_workspace_or_global() -> None:
    local = wire.parse_recipe({"name": "a", "source": "/proj/.kiro/workflows/a.workflow.json"})
    user = wire.parse_recipe({"name": "b", "source": "/home/u/.kiro/workflows/b.workflow.json"})
    sibling = wire.parse_recipe({"name": "c", "source": "/proj-2/.kiro/workflows/c.workflow.json"})
    assert local and user and sibling
    assert local.to_payload("/proj")["group"] == "workspace"
    assert user.to_payload("/proj")["scope"] == "global"
    assert sibling.to_payload("/proj")["scope"] == "global", "a prefix is not a parent"


def test_a_repeat_carries_its_stop_config() -> None:
    [node] = wire.parse_plan(
        [{"type": "repeat", "id": "l", "maxIterations": 3, "stopWhen": "LGTM", "steps": []}]
    )
    payload = node.to_payload()
    assert payload["stopWhen"] == "LGTM" and payload["maxIterations"] == 3


# ── Gap 10C: the event row's English line ──


@pytest.mark.parametrize(
    "seconds,text", [(None, None), (9, "9s"), (76, "1m 16s"), (3720, "1h 2m"), (-1, None)]
)
def test_format_duration(seconds, text) -> None:
    assert format_duration(seconds) == text


def test_event_line_words_outcomes_and_notes() -> None:
    assert (
        event_line({"type": "outcome", "name": "r", "status": "completed", "durationSecs": 76})
        == "Workflow r completed in 1m 16s."
    )
    assert event_line({"type": "outcome", "name": "r", "status": "aborted"}) == (
        "Workflow r was stopped."
    )
    assert (
        event_line(
            {
                "type": "note",
                "name": "r",
                "severity": "warning",
                "step": "Plan",
                "agent": "wf-planner",
                "message": "Which\nbranch?",
            }
        )
        == "Waiting for you: Plan (wf-planner) in workflow r: Which branch?"
    )


# ── Router fakes ──


class FakeRuntime:
    acp_backend = "kas"

    def __init__(self, methods: list[str] | None = None) -> None:
        self.alive = True
        self.calls: list[tuple[str, dict]] = []
        self.responses: list[tuple[Any, dict]] = []
        self.answers: dict[str, Any] = {}
        self._work_dir = "/tmp"
        self._agent_capabilities: dict = (
            {"_meta": {"kiro": {"extensionMethods": methods}}} if methods is not None else {}
        )

    def is_alive(self) -> bool:
        return self.alive

    async def extension_request(self, method, params, *, timeout=30.0):
        self.calls.append((method, params))
        answer = self.answers.get(method, {})
        if isinstance(answer, Exception):
            raise answer
        return answer(params) if callable(answer) else answer

    async def send_response(self, request_id, result):
        self.responses.append((request_id, result))

    async def send_notification(self, method, params):
        pass

    async def kill(self, *, expected=False, reason=""):
        self.alive = False


class _Hooks:
    def __init__(self) -> None:
        self.notes: list[tuple[str, str, str, dict]] = []
        self.retired: list[str] = []
        self.rows: list[tuple[str, str, dict]] = []

    def hooks(self) -> DashboardHooks:
        return DashboardHooks(
            notify=lambda key, slot, title, _body, meta: self.notes.append(
                (key, slot, title, dict(meta))
            ),
            retire=self.retired.append,
            append_marker=lambda slot, content, meta: bool(
                self.rows.append((slot, content, dict(meta))) or True
            ),
        )


async def _no_refusal(_cmd: str):
    return None


def _router(chat_rt: FakeRuntime | None = None, **kwargs) -> KasWorkflowRouter:
    async def factory():
        return chat_rt or FakeRuntime()

    audited = kwargs.pop("audited", None)
    return KasWorkflowRouter(
        factory,
        screen=lambda _c: None,
        name_grant=_no_refusal,
        audit=(lambda **f: audited.append(f)) if audited is not None else (lambda **f: None),
        clock=_clock,
        **kwargs,
    )


async def _started(tmp_path, chat_rt: FakeRuntime, router: KasWorkflowRouter):
    chat_rt.answers[wire.METHOD_NEW] = {
        "workflowId": "w1",
        "initialState": {
            "status": "running",
            "workflowName": "investigate",
            "parentSessionId": "acp-parent",
        },
    }
    sink = router.sink_for(chat_rt)
    launch = router.prepare_launch("chat-1", str(tmp_path), "bundled://investigate", {})
    await router.start_in_session(chat_rt, "acp-parent", launch)
    sink.on_notification(
        wire.NOTIFY_RUN_START,
        {"workflowId": "w1", "nodeTree": [{"type": "step", "id": "a", "label": "Plan"}]},
    )
    sink.on_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "w1", "nodeId": "a", "type": "step", "sessionId": "step-1"},
    )
    return sink


def _perm(command: str | None = "ls", session: str = "step-1") -> dict:
    raw = {"command": command} if command is not None else {}
    return {
        "sessionId": session,
        "toolCall": {"title": "Run it", "rawInput": raw},
        "options": [
            {"optionId": "yes", "kind": "allow_once", "name": "Allow"},
            {"optionId": "always", "kind": "allow_always", "name": "Always allow"},
            {"optionId": "no", "kind": "reject_once", "name": "Reject"},
        ],
    }


ALLOW_ONCE = {"outcome": {"outcome": "selected", "optionId": "yes"}}


# ── Gap 20: gating on the driving process (router) ──


@pytest.mark.asyncio
async def test_actions_the_driver_does_not_advertise_are_withdrawn_and_refused(tmp_path):
    chat_rt = FakeRuntime(methods=[wire.METHOD_NEW, wire.METHOD_INVOKE, wire.METHOD_CANCEL])
    router = _router(chat_rt)
    sink = await _started(tmp_path, chat_rt, router)
    sink.on_notification(
        wire.NOTIFY_NODE_PAUSED, {"workflowId": "w1", "nodeId": "a", "reason": "x"}
    )
    row = router.run_row("w1")
    assert row is not None
    assert row["actions"] == ["cancel"], "no pause/resume advertised"
    assert row["nodes"][0]["actions"] == ["reply"], "update (complete/fail) not advertised"
    with pytest.raises(KasWorkflowError, match="does not support pause"):
        await router.control("w1", "pause")
    with pytest.raises(KasWorkflowError, match="does not support ending a step"):
        await router.end_step("w1", "a", "completed")
    assert all(m != wire.METHOD_PAUSE for m, _ in chat_rt.calls)


@pytest.mark.asyncio
async def test_a_legacy_driver_keeps_every_action(tmp_path):
    chat_rt = FakeRuntime(methods=None)
    router = _router(chat_rt)
    await _started(tmp_path, chat_rt, router)
    assert router.run_row("w1")["actions"] == ["pause", "cancel"]  # type: ignore[index]


# ── Gap 6: bell notes for held asks, retired once answered ──


@pytest.mark.asyncio
async def test_a_held_approval_raises_one_note_and_retires_it_on_answer(tmp_path):
    chat_rt = FakeRuntime()
    router = _router(chat_rt)
    seen = _Hooks()
    router.set_dashboard_hooks(seen.hooks())
    sink = await _started(tmp_path, chat_rt, router)
    sink.on_step_request(11, "step-1", PERM, _perm("npm test"))
    await async_wait_until(lambda: router.approvals())
    [approval] = router.approvals()
    [(key, slot, title, meta)] = seen.notes
    assert (key, slot) == (approval["key"], "chat-1")
    assert title == "investigate · Plan is waiting for your approval"
    assert meta == {"workflowId": "w1", "nodeId": "a", "ask": "approval"}
    await router.answer(approval["key"], "yes")
    assert seen.retired == [approval["key"]]


@pytest.mark.asyncio
async def test_an_ask_withdrawn_by_a_mode_change_retires_its_note(tmp_path):
    chat_rt = FakeRuntime()
    router = _router(chat_rt)
    seen = _Hooks()
    router.set_dashboard_hooks(seen.hooks())
    sink = await _started(tmp_path, chat_rt, router)
    mode = ["normal"]

    class _Provider:
        class client:  # noqa: N801 -- a stand-in attribute bag
            runtime = chat_rt
            session_id = "acp-parent"

    router.note_chat_session("chat-1", _Provider(), lambda: (mode[0], mode[0]))
    sink.on_step_request(12, "step-1", PERM, _perm("npm test"))
    await async_wait_until(lambda: router.approvals())
    key = router.approvals()[0]["key"]
    mode[0] = "trust"
    assert await router.recheck_pending() == 1
    assert seen.retired == [key]


# ── Gap 21: "Always allow" is a Crew grant for this step only ──


@pytest.mark.asyncio
async def test_always_allow_is_a_crew_grant_for_this_step_and_command(tmp_path):
    chat_rt = FakeRuntime()
    audited: list[dict] = []
    router = _router(chat_rt, audited=audited)
    sink = await _started(tmp_path, chat_rt, router)
    sink.on_step_request(21, "step-1", PERM, _perm("npm test"))
    await async_wait_until(lambda: router.approvals())
    [approval] = router.approvals()
    always = next(o for o in approval["options"] if o["kind"] == "allow_always")
    assert always["grant"] == {"scope": "step", "match": "command"}
    await router.answer(approval["key"], "always")
    # KAS is told allow-ONCE: nothing persists there to skip Crew's screen.
    assert chat_rt.responses == [(21, ALLOW_ONCE)]
    # The same command in the same step is allowed by Crew, and logged.
    sink.on_step_request(22, "step-1", PERM, _perm("npm test"))
    await async_wait_until(lambda: len(chat_rt.responses) == 2)
    assert chat_rt.responses[-1] == (22, ALLOW_ONCE)
    assert audited[-1]["metadata"]["reason"] == "step_always"
    # Another command still asks.
    sink.on_step_request(23, "step-1", PERM, _perm("rm build"))
    await async_wait_until(lambda: router.approvals())
    assert len(chat_rt.responses) == 2


@pytest.mark.asyncio
async def test_always_allow_does_not_reach_another_step(tmp_path):
    chat_rt = FakeRuntime()
    router = _router(chat_rt)
    sink = await _started(tmp_path, chat_rt, router)
    sink.on_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "w1", "nodeId": "b", "type": "step", "sessionId": "step-2"},
    )
    sink.on_step_request(31, "step-1", PERM, _perm("npm test"))
    await async_wait_until(lambda: router.approvals())
    await router.answer(router.approvals()[0]["key"], "always")
    sink.on_step_request(32, "step-2", PERM, _perm("npm test", session="step-2"))
    await async_wait_until(lambda: router.approvals())
    assert len(chat_rt.responses) == 1, "step-2 has no grant"


def test_option_grants_say_what_a_click_grants() -> None:
    from kiro_crew.kas_workflows.router import _option_grant

    assert _option_grant("allow_once", True) == {"scope": "once", "match": None}
    assert _option_grant("allow_always", False) == {"scope": "step", "match": "tool"}
    assert _option_grant("reject_always", True) == {"scope": "step", "match": None}


# ── Gap 10B: outcome and note rows, always ──


@pytest.mark.asyncio
async def test_a_run_end_writes_one_outcome_row(tmp_path):
    chat_rt = FakeRuntime()
    router = _router(chat_rt)
    seen = _Hooks()
    router.set_dashboard_hooks(seen.hooks())
    sink = await _started(tmp_path, chat_rt, router)
    done = {"workflowId": "w1", "status": "completed", "finalState": {}}
    sink.on_notification(wire.NOTIFY_RUN_COMPLETE, done)
    sink.on_notification(wire.NOTIFY_RUN_COMPLETE, done)
    [(slot, content, meta)] = seen.rows
    assert slot == "chat-1"
    assert content == "Workflow investigate completed in 0s."
    event = meta["kas_event"]
    assert (event["type"], event["status"], event["durationSecs"]) == ("outcome", "completed", 0)
    # Running again makes its next end a new one.
    sink.on_notification(wire.NOTIFY_RUN_START, {"workflowId": "w1"})
    sink.on_notification(wire.NOTIFY_RUN_COMPLETE, {**done, "status": "failed"})
    assert [r[2]["kas_event"]["status"] for r in seen.rows] == ["completed", "failed"]


@pytest.mark.asyncio
async def test_every_step_note_to_the_parent_writes_a_row(tmp_path):
    chat_rt = FakeRuntime()
    router = _router(chat_rt)
    seen = _Hooks()
    router.set_dashboard_hooks(seen.hooks())
    sink = await _started(tmp_path, chat_rt, router)
    assert router._turn_starter is None, "no observe turn can start: the row still lands"
    for severity in ("success", "error"):
        sink.on_watched_notification(
            wire.NOTIFY_SESSION_NOTIFY,
            {
                "workflowId": "w1",
                "sessionId": "acp-parent",
                "severity": severity,
                "nodeId": "a",
                "agentName": "wf-planner",
                "message": "done with step a",
            },
        )
    # A note to someone other than the parent is not the chat's.
    sink.on_watched_notification(
        wire.NOTIFY_SESSION_NOTIFY,
        {"workflowId": "w1", "sessionId": "step-1", "severity": "info", "message": "x"},
    )
    assert [r[2]["kas_event"]["severity"] for r in seen.rows] == ["success", "error"]
    assert seen.rows[1][1] == "Error: Plan (wf-planner) in workflow investigate: done with step a"
    assert seen.rows[0][2]["kas_event"]["step"] == "Plan"


# ── Gap 16: recipes revision ──


def test_recipes_changed_bumps_the_recipes_revision() -> None:
    router = _router()
    sink = router.sink_for(FakeRuntime())
    sink.on_notification(wire.NOTIFY_RECIPES_CHANGED, {"recipes": []})
    assert router.recipes_revision == 1


# ── Gap 27: recipe detail from the listed file only ──


@pytest.mark.asyncio
async def test_recipe_detail_reads_the_listed_json_definition(tmp_path):
    recipe_file = tmp_path / ".kiro" / "workflows" / "r.workflow.json"
    recipe_file.parent.mkdir(parents=True)
    recipe_file.write_text(
        json.dumps(
            {
                "name": "r",
                "steps": [
                    {"type": "step", "id": "plan", "agent": "wf-planner", "prompt": "Plan it."},
                    {
                        "type": "repeat",
                        "id": "loop",
                        "steps": [{"type": "step", "id": "code", "prompt": "Code it."}],
                    },
                ],
            }
        )
    )
    rt = FakeRuntime()
    rt.answers[wire.METHOD_LIST_RECIPES] = {
        "recipes": [{"name": "r", "source": str(recipe_file)}, {"name": "g", "builtIn": True}]
    }
    router = _router(rt)
    detail = await router.recipe_detail(str(tmp_path), str(recipe_file))
    assert detail["definitionAvailable"] is True
    assert [(s["nodeId"], s["prompt"]) for s in detail["steps"]] == [
        ("plan", "Plan it."),
        ("code", "Code it."),
    ]
    assert detail["recipe"]["group"] == "workspace"
    bundled = await router.recipe_detail(str(tmp_path), "bundled://g")
    assert bundled["definitionAvailable"] is False and bundled["steps"] == []
    with pytest.raises(KasWorkflowError, match="not listed"):
        await router.recipe_detail(str(tmp_path), str(tmp_path / "other.workflow.json"))


# ── Gap 19 (router): a deleted run is dropped and never chased ──


@pytest.mark.asyncio
async def test_a_run_deleted_elsewhere_leaves_the_project_list(tmp_path):
    rt = FakeRuntime()
    router = _router(rt)
    lists = [
        {
            "runs": [
                {"workflowId": "a", "status": "paused"},
                {"workflowId": "b", "status": "paused"},
            ]
        },
        {"runs": [{"workflowId": "a", "status": "paused"}]},
    ]
    rt.answers[wire.METHOD_LIST] = lambda _p: lists.pop(0)
    router._ledger["b"] = {"slot": "chat-1", "cwd": "", "createdAt": 0.0, "ended": False}
    assert {r["workflowId"] for r in await router.runs(str(tmp_path))} == {"a", "b"}
    assert {r["workflowId"] for r in await router.runs(str(tmp_path))} == {"a"}
    assert "b" not in router.mirror
    assert router._ledger["b"]["ended"] is True, "a reconnect never chases it"
