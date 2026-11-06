"""Shared fakes for the KAS workflow parity tests (router and route tables)."""

from __future__ import annotations

from typing import Any

from kiro_crew.kas_workflows import wire
from kiro_crew.kas_workflows.router import KasWorkflowRouter


class FakeRuntime:
    acp_backend = "kas"

    def __init__(self) -> None:
        self.alive = True
        self.calls: list[tuple[str, dict]] = []
        self.notifications: list[tuple[str, dict]] = []
        self.responses: list[tuple[Any, dict]] = []
        self.answers: dict[str, Any] = {}
        self._work_dir = "/tmp"

    def is_alive(self) -> bool:
        return self.alive

    async def extension_request(self, method, params, *, timeout=30.0):
        self.calls.append((method, params))
        answer = self.answers.get(method, {})
        if isinstance(answer, Exception):
            raise answer
        return answer(params) if callable(answer) else answer

    async def send_response(self, request_id, result, *, withdrawal_check=None):
        if withdrawal_check is not None:
            withdrawal_check()  # under the write lock, as the runtime runs it
        self.responses.append((request_id, result))

    async def send_notification(self, method, params):
        self.notifications.append((method, params))

    async def kill(self, *, expected=False, reason=""):
        self.alive = False


class _Inner:
    def __init__(self, runtime, session_id):
        self.runtime = runtime
        self.session_id = session_id


class ChatProvider:
    def __init__(self, runtime, session_id="acp-parent"):
        self.client = _Inner(runtime, session_id)


def step_tool_call_frame(params: dict) -> dict:
    """The ``tool_call`` frame KAS sends on a step's session before its permission request.

    Shaped as recorded (``fixtures/acp_frames/kas``): the request's own title and
    ``rawInput``, ``kind`` ``execute`` for a call that names a command, else
    ``read`` (or ``edit`` when the request's consent is ``fs_write``).
    """
    tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    kiro = meta.get("kiro") if isinstance(meta.get("kiro"), dict) else {}
    raw = tool_call.get("rawInput")
    named = isinstance(raw, dict) and any(k in raw for k in ("command", "cmd", "script"))
    consent = kiro.get("consent") if isinstance(kiro.get("consent"), dict) else {}
    kind = (
        "execute"
        if named or "command" in kiro
        else ("edit" if consent.get("capability") == "fs_write" else "read")
    )
    frame_meta = {"toolOrigin": "default"}
    for key in ("mcpServerName", "toolName"):
        if key in kiro:
            frame_meta[key] = kiro[key]
    frame = {
        "sessionUpdate": "tool_call",
        "toolCallId": tool_call.get("toolCallId") or "tc-step",
        "title": tool_call.get("title") or "Tool call",
        "kind": kind,
        "status": "pending",
        "_meta": {"kiro": frame_meta},
    }
    if raw is not None:
        frame["rawInput"] = raw
    if isinstance(tool_call.get("locations"), list):
        frame["locations"] = tool_call["locations"]
    return frame


def framed_identity(params, *, plan: bool = False):
    """The router's real identity path, as if the step's ``tool_call`` frame had arrived."""
    import copy

    from kiro_crew.kas_workflows.step_identity import StepToolCalls

    params = copy.deepcopy(dict(params))
    tool_call = params.get("toolCall")
    if isinstance(tool_call, dict) and not tool_call.get("toolCallId"):
        tool_call["toolCallId"] = "tc-step"
    calls = StepToolCalls()
    calls.observe(str(params.get("sessionId") or ""), step_tool_call_frame(params))
    return calls.event_for(params, plan=plan)


def make_router(**kwargs) -> KasWorkflowRouter:
    async def factory():
        return FakeRuntime()

    kwargs.setdefault("identity", framed_identity)
    return KasWorkflowRouter(factory, screen=kwargs.pop("screen", lambda _c: None), **kwargs)


def step_node(node_id: str, status: str, session: str | None = None, **extra) -> dict:
    node = {"nodeId": node_id, "type": "step", "status": status, **extra}
    if session:
        node["sessionId"] = session
    return node


def loop_state(status: str = "paused", **extra) -> dict:
    """A run whose repeat ``loop`` ran two passes of step ``code`` and hit its budget."""
    iterations = [
        {
            "nodeId": f"loop#{n}",
            "type": "sequence",
            "status": "completed",
            "iteration": n,
            "children": [step_node("code", "completed", f"s{n}")],
        }
        for n in range(2)
    ]
    return {
        "status": status,
        "workflowName": "review",
        "parentSessionId": "acp-parent",
        "pauseReason": "Repeat 'loop' reached maxIterations (2).",
        "pause": {
            "kind": "maxIterations",
            "nodeId": "loop",
            "maxIterations": 2,
            "iterationsRun": 2,
        },
        "root": {
            "nodeId": "w1",
            "type": "sequence",
            "status": status,
            "children": [
                {"nodeId": "loop", "type": "repeat", "status": "paused", "children": iterations}
            ],
        },
        **extra,
    }


LOOP_PLAN = [
    {
        "nodeId": "loop",
        "type": "repeat",
        "maxIterations": 2,
        "onMaxIterations": "pause",
        "steps": [{"nodeId": "code", "type": "step", "agentName": "coder"}],
    }
]


def driven(router: KasWorkflowRouter, state: dict, plan=None, wid: str = "w1") -> FakeRuntime:
    rt = FakeRuntime()
    sink = router.sink_for(rt)
    router.mirror.apply_load({"workflowId": wid, "state": state, "nodePlan": plan or []})
    # A notification is how the router learns which process drives a run.
    sink.on_notification(wire.NOTIFY_WATCH_POLL, {"workflowId": wid, "nodeId": "zz"})
    return rt


def failed_state() -> dict:
    return {
        "status": "failed",
        "workflowName": "x",
        "parentSessionId": "acp-parent",
        "root": {
            "nodeId": "w1",
            "type": "sequence",
            "status": "failed",
            "children": [
                step_node("a", "completed", "sa"),
                {
                    "nodeId": "loop",
                    "type": "repeat",
                    "status": "failed",
                    "children": [
                        {
                            "nodeId": "loop#0",
                            "type": "sequence",
                            "iteration": 0,
                            "status": "failed",
                            "children": [step_node("code", "failed", "s0")],
                        }
                    ],
                },
            ],
        },
    }


def parallel_state() -> dict:
    return {
        "status": "running",
        "workflowName": "x",
        "root": {
            "nodeId": "w1",
            "type": "parallel",
            "status": "running",
            "children": [step_node("a", "running", "sa"), step_node("b", "running", "sb")],
        },
    }


def question_request() -> dict:
    return {
        "sessionId": "sa",
        "toolCall": {"toolCallId": "tc-9", "title": "Which database?", "status": "pending"},
        "options": [
            {"optionId": "pg", "name": "Postgres", "kind": "allow_once"},
            {"optionId": "my", "name": "MySQL", "kind": "allow_once"},
        ],
    }


async def stop_step(router, session_id: str) -> None:
    """What ``cancel_step`` does to the router's own records: one mark, then withdraw."""
    router._mark_stopped(session_id)
    await router._withdraw_session_asks(session_id)
