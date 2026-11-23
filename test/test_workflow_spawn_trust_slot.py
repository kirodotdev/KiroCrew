"""A workflow step's spawn resolves to the slot of the chat that started the run.

The spawn approval callback auto-approves only when its slot resolver names a
trusted slot. A workflow step's session is ``<prefix>:<run_id>:...`` (per-call,
pooled, unpooled or memory-scoped worker), which no tab displays, so the resolver
must map it through the workflow registry to the run's recorded origin
``session_key``. An unknown run or a blank origin must resolve to ``""`` so the
spawn still prompts.

The tool prompts of a step's sub-agents are asked under the same step key, so the
tool-approval callback maps that key the same way: the prompt is raised in the
origin chat's tab and only that chat's Trust can answer it. The wiring tests at
the end drive the real ``_init_subagents`` and the real approval callbacks.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.llm_helpers import LLMEvent
from kiro_crew.slack import gateway as gw
from kiro_crew.workflows.registry import RunHandle, RunRegistry


def _service(*handles: RunHandle) -> SimpleNamespace:
    """A workflow service exposing a REAL registry, so ``get`` keeps its contract."""
    registry = RunRegistry(store=None)
    for handle in handles:
        registry.register(handle, persist=False)
    return SimpleNamespace(registry=registry)


@pytest.mark.parametrize(
    "parent",
    [
        "wf:run1:0",
        "wf:run1:12",
        "wf-pool:run1:w3",
        "wf-unpooled:run1:0",
        "wf-worker:run1:" + "a" * 64,
    ],
)
def test_step_parent_resolves_to_origin_slot(parent: str) -> None:
    svc = _service(RunHandle(run_id="run1", name="w", session_key="dashboard:chat-7"))
    expected = gw.subagent_event_slot("dashboard:chat-7")
    assert expected == "chat-7"
    assert gw._spawn_parent_slot(parent, svc) == expected


def test_unknown_run_prompts() -> None:
    svc = _service(RunHandle(run_id="run1", name="w", session_key="dashboard:chat-7"))
    assert gw._spawn_parent_slot("wf:other:0", svc) == ""
    assert gw._spawn_parent_slot("wf-pool:other:w0", svc) == ""


def test_blank_origin_prompts() -> None:
    svc = _service(
        RunHandle(run_id="blank", name="w", session_key=""),
        RunHandle(run_id="none", name="w", session_key=None),  # type: ignore[arg-type]
    )
    assert gw._spawn_parent_slot("wf:blank:0", svc) == ""
    assert gw._spawn_parent_slot("wf:none:0", svc) == ""


def test_no_workflow_service_prompts() -> None:
    assert gw._spawn_parent_slot("wf:run1:0", None) == ""


def test_registry_failure_prompts() -> None:
    def _boom(run_id: str) -> RunHandle:
        raise RuntimeError(f"registry down for {run_id}")

    svc = SimpleNamespace(registry=SimpleNamespace(get=MagicMock(side_effect=_boom)))
    assert gw._spawn_parent_slot("wf:run1:0", svc) == ""
    svc.registry.get.assert_called_once_with("run1")


def test_non_wf_parent_unchanged() -> None:
    def _no_lookup(run_id: str) -> RunHandle:
        raise AssertionError(f"non-wf key looked up run {run_id}")

    svc = SimpleNamespace(registry=SimpleNamespace(get=MagicMock(side_effect=_no_lookup)))
    for key in ("dashboard:chat-7", "cron:job1", "slack:1785370133.085469", "wf-author:x:0"):
        assert gw._spawn_parent_slot(key, svc) == gw.subagent_event_slot(key)
    assert gw._spawn_parent_slot("", svc) == ""
    svc.registry.get.assert_not_called()


#: One step-session key per executor, all of run ``r1``.
_STEP_KEYS = [
    pytest.param("wf:r1:0", id="wf"),
    pytest.param("wf-pool:r1:w0", id="wf-pool"),
    pytest.param("wf-unpooled:r1:0", id="wf-unpooled"),
    pytest.param("wf-worker:r1:" + "a" * 64, id="wf-worker"),
]

#: Bounds every awaited approval, so a lost answer fails the test instead of hanging it.
_ASK_TIMEOUT_SECS = 30.0


class _Tab:
    """A dashboard tab with only the state the approval callback reads."""

    def __init__(self, *, trust: bool = False) -> None:
        self.running = True
        self._trust = trust
        self._trust_scope = ""


class _Wired:
    """The real ``_init_subagents`` wiring around the real approval callbacks.

    Only ``SubagentManager`` is replaced, by a capture, so ``on_tool_approval`` and
    ``on_spawn_approval`` are the closures the gateway ships. The dashboard is a
    double whose ``request_approval`` records the tab each prompt is raised in and
    answers no; its workflow service holds a real registry. ``child1`` is a
    sub-agent of *parent*, the session key every call here is asked under.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        parent: str,
        *,
        tabs: dict[str, _Tab],
        service: SimpleNamespace,
    ) -> None:
        self.parent = parent
        self.manager_kwargs: dict[str, Any] = {}
        manager = MagicMock(name="subagent_mgr")
        manager.get = MagicMock(
            side_effect=lambda agent_id: (
                SimpleNamespace(parent_session_key=parent) if agent_id == "child1" else None
            )
        )

        def _capture(**kwargs: Any) -> MagicMock:
            self.manager_kwargs = kwargs
            return manager

        monkeypatch.setattr(gw, "SubagentManager", _capture)
        monkeypatch.setattr(gw, "safety_override", lambda: SimpleNamespace(is_active=lambda: False))
        monkeypatch.setattr(gw, "deliver_spawn_approval", AsyncMock(return_value=None))
        orch = gw.GatewayOrchestrator(
            KiroCrewConfig(),
            no_crons=True,
            no_open=True,
            credentials={gw.CRED_OWNER_ID: "U_OWNER"},
        )
        orch.sessions = MagicMock(name="sessions")
        orch.ctx_builder = MagicMock(name="ctx_builder")
        orch.slack = None
        self.state = MagicMock(name="dashboard_state")
        self.state._slots = tabs
        self.state.workflow_service = service
        self.state.request_approval = AsyncMock(return_value=False)
        orch.dashboard_state = self.state
        orch._init_subagents()

    async def ask_tool(self, event: LLMEvent) -> bool:
        """The child's tool call, asked the way its permission ladder asks it."""
        approve = self.manager_kwargs["on_tool_approval"]
        return bool(await asyncio.wait_for(approve(event, self.parent), _ASK_TIMEOUT_SECS))

    async def ask_spawn(self) -> None:
        """The step's spawn of ``child1``, asked the way the admission gate asks it."""
        approve = self.manager_kwargs["on_spawn_approval"]
        await asyncio.wait_for(approve("spawn:child1", "run it", self.parent), _ASK_TIMEOUT_SECS)

    def prompt_slots(self) -> list[str]:
        """The tab each raised prompt named, in order; ``""`` is the global feed only."""
        return [call.kwargs["slot"] for call in self.state.request_approval.await_args_list]


def _tool_event(*, low_fidelity: bool = False) -> LLMEvent:
    """A child's tool request. Its id is the backend's JSON-RPC id, which names no agent."""
    extra: dict[str, Any] = {"sub_session_id": "child-a"} if low_fidelity else {}
    event = LLMEvent(
        kind="permission_request", request_id="1", title="Running: git status", **extra
    )
    assert event.child_low_fidelity is low_fidelity
    return event


def _origin(session_key: str) -> SimpleNamespace:
    """A workflow service whose run ``r1`` was started from *session_key*."""
    return _service(RunHandle(run_id="r1", name="w", session_key=session_key))


@pytest.mark.asyncio
@pytest.mark.parametrize("step", _STEP_KEYS)
async def test_child_tool_prompt_is_raised_in_the_origin_chat(
    step: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _Wired(monkeypatch, step, tabs={"main": _Tab()}, service=_origin("dashboard:main"))

    assert await wired.ask_tool(_tool_event()) is False
    assert wired.prompt_slots() == ["main"]


@pytest.mark.asyncio
async def test_child_tool_call_rides_the_origin_chats_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _Wired(
        monkeypatch,
        "wf-pool:r1:w0",
        tabs={"main": _Tab(trust=True)},
        service=_origin("dashboard:main"),
    )

    assert await wired.ask_tool(_tool_event()) is True
    assert wired.prompt_slots() == []


@pytest.mark.asyncio
async def test_another_chats_trust_never_answers_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run's origin decides: a trusted bystander tab neither hosts nor approves the prompt."""
    wired = _Wired(
        monkeypatch,
        "wf-pool:r1:w0",
        tabs={"main": _Tab(trust=True), "other": _Tab()},
        service=_origin("dashboard:other"),
    )

    assert await wired.ask_tool(_tool_event()) is False
    assert wired.prompt_slots() == ["other"]


@pytest.mark.asyncio
async def test_origin_trust_does_not_approve_an_unverified_child_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _Wired(
        monkeypatch,
        "wf-pool:r1:w0",
        tabs={"main": _Tab(trust=True)},
        service=_origin("dashboard:main"),
    )

    assert await wired.ask_tool(_tool_event(low_fidelity=True)) is False
    assert wired.prompt_slots() == ["main"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_id", "origin"),
    [("other", "dashboard:main"), ("r1", "")],
    ids=["unknown-run", "blank-origin"],
)
async def test_child_tool_prompt_with_no_known_origin_stays_on_the_global_feed(
    run_id: str, origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _Wired(
        monkeypatch,
        "wf-pool:r1:w0",
        tabs={"main": _Tab(trust=True)},
        service=_service(RunHandle(run_id=run_id, name="w", session_key=origin)),
    )

    assert await wired.ask_tool(_tool_event()) is False
    assert wired.prompt_slots() == [""]


@pytest.mark.asyncio
async def test_the_step_childs_spawn_and_tool_prompts_share_a_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _Wired(
        monkeypatch, "wf-pool:r1:w0", tabs={"main": _Tab()}, service=_origin("dashboard:main")
    )

    await wired.ask_spawn()
    await wired.ask_tool(_tool_event())

    assert wired.prompt_slots() == ["main", "main"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent", "slot"),
    [("dashboard:main", "main"), ("slack:1785370133.085469", "")],
    ids=["chat", "channel-without-tab"],
)
async def test_other_parent_keys_keep_their_own_tab_lookup(
    parent: str, slot: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _Wired(monkeypatch, parent, tabs={"main": _Tab()}, service=_origin("dashboard:main"))

    await wired.ask_tool(_tool_event())

    assert wired.prompt_slots() == [slot]
