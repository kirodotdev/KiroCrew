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

    async def send_response(self, request_id, result):
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


def make_router(**kwargs) -> KasWorkflowRouter:
    async def factory():
        return FakeRuntime()

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
