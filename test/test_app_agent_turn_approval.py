"""An app turn's tool call the hook gate does not settle goes to the owner's prompt.

``AgentTurnSDK.run`` (a Meetings note taker, any ``ctx.turns`` app) runs
``HOOK_BASED``. A call the gate neither denies nor grants reaches the approver
the turn passes. The gateway registers its approval prompt, the one a cron
job's call uses, and every app turn hands it to the turn as ``on_tool_approval``,
so the call is not left to ``stream_and_collect``'s caller-less default approve.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import (
    _make_orchestrator,
    _mock_context_builder,
    _mock_dashboard_state,
    _mock_sessions,
)

from kiro_crew import llm_helpers, name_grant
from kiro_crew.apps import agent_turn_sdk
from kiro_crew.apps.agent_turn_sdk import AgentTurnSDK
from kiro_crew.hooks import HookManager
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.shared_crew import turn_gate
from kiro_crew.slack.gateway_runtime.tool_policy import _BACKGROUND_APPROVAL_SOURCES

APP = "turn-probe"
#: Names a hang only; every awaited turn here finishes on its own signal.
_BOUND = 30.0


class _Provider:
    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)

    async def steer(self, *_args: object, **_kwargs: object) -> None:
        return None


class _Manager:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider
        self.agents: dict[str, str] = {}

    async def get_or_create(self, key: str, agent: str | None = None, **_kwargs: Any):
        self.agents[key] = agent or ""
        return self.provider, True, False

    def get_agent(self, key: str) -> str:
        return self.agents.get(key, "")

    def release(self, key: str) -> None:
        return None


@pytest.fixture(autouse=True)
def _turn_seams(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Default agent and admission are fixed; no gateway prompt is registered."""
    _floor_monkeypatch.setattr(agent_turn_sdk, "_default_agent", lambda: "host-default")

    async def _admit(work: Any, **_kwargs: Any) -> Any:
        return await work

    _floor_monkeypatch.setattr(turn_gate, "run_admitted_or_raise", _admit)
    _floor_monkeypatch.setattr(agent_turn_sdk, "_turn_approver", None, raising=False)


def _register(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    """Register *factory* as the gateway would; undone after the test."""
    monkeypatch.setattr(agent_turn_sdk, "_turn_approver", factory, raising=False)


@pytest.fixture
def one_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each turn asks permission for one shell call that is not read-only.

    The stream is replaced by the real permission resolver, called with exactly
    the policy, gate and approver the SDK handed the stream.
    """

    async def _clean(_event: object) -> None:
        return None

    async def _stream(provider: Any, _message: str, **kwargs: Any) -> str:
        event = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Running: mkdir out",
            request_id="r1",
            tool_input=json.dumps({"command": "mkdir out"}),
            is_shell=True,
        )
        with (
            patch("kiro_crew.sel.sel", lambda: MagicMock()),
            patch.object(name_grant, "refusal_for_event", _clean),
        ):
            await llm_helpers._resolve_permission(
                provider,
                event,
                kwargs["approval_policy"],
                kwargs["hooks"],
                on_tool_approval=kwargs.get("on_tool_approval"),
                session_key=kwargs["session_key"],
                agent=kwargs["agent"],
                app=kwargs["app"],
            )
        return "reply"

    monkeypatch.setattr(llm_helpers, "stream_and_collect", _stream)


def _run_turn() -> _Provider:
    provider = _Provider()
    sdk = AgentTurnSDK(APP, lambda: _Manager(provider), HookManager)
    asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-n-1", "a line"), _BOUND))
    return provider


def test_an_unsettled_call_asks_the_registered_prompt_and_its_no_rejects(
    monkeypatch, one_tool_call
):
    asked: list[tuple[str, str]] = []

    def _factory(app: str):
        async def _approve(event: Any) -> bool:
            asked.append((app, event.request_id))
            return False

        return _approve

    _register(monkeypatch, _factory)
    provider = _run_turn()

    assert asked == [(APP, "r1")]
    assert (provider.approved, provider.rejected) == ([], ["r1"])


def test_an_unsettled_call_the_prompt_approves_runs(monkeypatch, one_tool_call):
    async def _approve(_event: Any) -> bool:
        return True

    _register(monkeypatch, lambda _app: _approve)
    provider = _run_turn()

    assert (provider.approved, provider.rejected) == (["r1"], [])


def test_control_with_no_prompt_registered_the_turn_keeps_todays_default(one_tool_call):
    """A bare application registers no prompt; the turn behaves as before."""
    provider = _run_turn()

    assert (provider.approved, provider.rejected) == (["r1"], [])


def test_the_gateway_registers_its_cron_style_prompt_for_app_turns(monkeypatch):
    orch = _make_orchestrator()
    orch.sessions = _mock_sessions()
    orch.ctx_builder = _mock_context_builder()
    orch.conv_log = MagicMock()
    orch.consolidator = MagicMock()
    orch.dashboard_state = _mock_dashboard_state()
    calls: list[tuple[str, dict[str, Any]]] = []
    callback = object()

    def _interactive_approval(source: str, **kwargs: Any) -> object:
        calls.append((source, kwargs))
        return callback

    with patch("kiro_crew.slack.gateway.TaskRunner"):
        orch._init_task_runner()
    monkeypatch.setattr(orch, "_interactive_approval", _interactive_approval)

    assert agent_turn_sdk._turn_approver("meetings") is callback
    assert calls == [("app", {"requester": "app meetings"})]
    assert calls == [("app", {"requester": "app meetings"})]


def test_an_app_prompt_takes_the_unattended_approval_window():
    """Nobody may be watching a meeting's agents; the prompt denies fast, as a cron's."""
    assert "app" in _BACKGROUND_APPROVAL_SOURCES


def test_a_cancelled_turn_stops_waiting_on_its_open_slack_prompt():
    """A turn past its budget is cancelled while its prompt is open on Slack.

    The cancellation reaches the caller, and no second, dashboard-only prompt
    is opened for a turn that already ended.
    """
    orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
    slack = MagicMock()
    slack.open_dm = AsyncMock(return_value="D_U1")
    slack.post_blocks = AsyncMock(return_value="ts")
    slack.update_message = AsyncMock()
    orch.slack = slack
    orch.sessions = _mock_sessions()
    dashboard = _mock_dashboard_state()
    dashboard._yolo = False
    dashboard._slots = {}
    asked = asyncio.Event()
    calls: list[str] = []

    async def _request_approval(approval_id: str, *_args: Any, **_kwargs: Any) -> bool:
        calls.append(approval_id)
        asked.set()
        await asyncio.Event().wait()
        return False

    dashboard.request_approval = _request_approval
    dashboard.resolve_approval = MagicMock()
    orch.dashboard_state = dashboard
    callback = orch._interactive_approval("app", requester="app meetings")
    event = MagicMock()
    event.request_id = "r1"
    event.title = "Running: mkdir out"
    event.tool_input = ""
    event.tool_purpose = ""

    async def _run() -> None:
        task = asyncio.ensure_future(callback(event))
        await asyncio.wait_for(asked.wait(), _BOUND)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _BOUND)

    with (
        patch("kiro_crew.slack.gateway.sel", lambda: MagicMock()),
        patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False),
        patch("kiro_crew.slack.handler._build_approval_blocks", return_value=[]),
        patch("kiro_crew.slack.handler._pending_approvals", {}),
    ):
        asyncio.run(_run())

    assert len(calls) == 1
    dashboard.resolve_approval.assert_called_once()
