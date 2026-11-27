"""The gate's ``app-own-tools`` row: an app's own agent calling that app's declared tools.

Pinned here: the row approves only when the gateway's app stamp, the agent that
actually ran and the tool's declaring app are one first-party app; every other caller,
agent, tool and stamp keeps its card; a cron run hands the gate its job's host-written
stamp and its resolved agent, never the job's agent name.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import hooks
from kiro_crew.apps import app_agent_tools, own_agent_tools
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, HookManager, HooksConfig, ToolCall
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect
from kiro_crew.mcp_tools.catalog import app_tools
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, LLMEvent

_TIER = "app-own-tools"
#: The app, its shipped agent and its declared tool; and a second app with its own.
_APP, _AGENT, _TOOL = "clock", "clock-keeper", "clock_api"
_OTHER_APP, _OTHER_AGENT, _OTHER_TOOL = "tunes", "tunes-dj", "tunes_api"
#: A core tool's name: an app that declares it does not own it.
_CORE_TOOL = "send_message"


def _declared(app: str, name: str) -> Any:
    return SimpleNamespace(app=app, name=name)


@pytest.fixture(autouse=True)
def _world(_floor_monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Two first-party apps, each with one agent and one declared tool, both enabled."""
    monkeypatch = _floor_monkeypatch
    state: dict[str, Any] = {
        "tools": [
            _declared(_APP, _TOOL),
            _declared(_APP, _CORE_TOOL),
            _declared(_OTHER_APP, _OTHER_TOOL),
        ],
        "enabled": {_APP: True, _OTHER_APP: True},
    }
    monkeypatch.setattr(hooks, "_BUILTIN_APP_AGENTS", hooks._BUILTIN_APP_AGENTS)
    hooks.set_builtin_app_agents(
        {
            _AGENT: _APP,
            f"{_APP}--{_AGENT}": _APP,
            _OTHER_AGENT: _OTHER_APP,
        }
    )
    monkeypatch.setattr(app_agent_tools, "declared_tools", lambda: tuple(state["tools"]))
    monkeypatch.setattr(app_tools, "app_enabled_state", lambda app: state["enabled"].get(app))
    yield state


def _call(tool: str = _TOOL, *, agent: str = _AGENT, **fields: Any) -> ToolCall:
    values: dict[str, Any] = {
        "title": f"Running: @kirocrew-core/{tool}",
        "mcp_server": "kirocrew-core",
        "mcp_tool": tool,
        "identity_trusted": True,
        "resolved_agent": agent,
    }
    values.update(fields)
    return ToolCall(**values)


def _judge(
    call: ToolCall, app: str = _APP, session_key: str = "dashboard:s1", **config: Any
) -> Any:
    return HookManager(HooksConfig(**config)).judge(call, app=app, session_key=session_key)


def test_the_apps_own_agent_calls_its_own_tool_without_a_card() -> None:
    for call in (_call(), _call(agent=f"{_APP}--{_AGENT}")):
        verdict = _judge(call)
        assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, _TIER)


@pytest.mark.parametrize(
    ("call", "app"),
    [
        # Another app's tool, from this app's own agent.
        (_call(_OTHER_TOOL), _APP),
        # This app's tool, from another app's agent, under either app's stamp.
        (_call(agent=_OTHER_AGENT), _APP),
        (_call(agent=_OTHER_AGENT), _OTHER_APP),
        # An agent no shipped manifest declares: the owner's own, whatever alias named it.
        (_call(agent="kirocrew"), _APP),
        (_call(agent="default"), _APP),
        (_call(agent=""), _APP),
        # A tool no manifest declares, a core tool the app also names, another server.
        (_call("clock_reset"), _APP),
        (_call(_CORE_TOOL), _APP),
        (_call(mcp_server=f"{_APP}:srv"), _APP),
        # An identity the backend did not vouch for, and a shell call titled like the tool.
        (_call(identity_trusted=False), _APP),
        (
            ToolCall(
                title=f"Running: @kirocrew-core/{_TOOL}",
                is_shell=True,
                command="ls",
                resolved_agent=_AGENT,
            ),
            _APP,
        ),
    ],
)
def test_the_grant_reaches_no_other_agent_tool_or_app(call: ToolCall, app: str) -> None:
    assert _judge(call, app=app).tier != _TIER


def test_a_run_the_gateway_did_not_stamp_keeps_its_card() -> None:
    """The agent alone proves nothing: a person's session running the app's agent asks."""
    assert _judge(_call(), app="").tier != _TIER
    assert _judge(_call(agent=""), app="").tier != _TIER


def test_a_cron_run_is_judged_on_its_stamp_and_the_agent_that_ran() -> None:
    def judge(app_run: tuple[str, str], **surface: Any) -> Any:
        call = _call(agent="", app_run=app_run)
        return HookManager().judge(call, session_key="cron:job-1", **surface)

    assert judge((_APP, _AGENT)).tier == _TIER
    for app_run in [("", _AGENT), (_APP, ""), (_APP, "kirocrew"), (_OTHER_APP, _AGENT)]:
        assert judge(app_run).tier != _TIER, app_run
    # A surface that names an app is judged on that app, never on the run's stamp.
    assert judge((_APP, _AGENT), app=_OTHER_APP).tier != _TIER
    # The two sources are never mixed: a stamp from one and an agent from the other.
    assert judge((_APP, _AGENT), app=_APP).tier != _TIER
    mixed = _call(agent=_AGENT, app_run=(_APP, ""))
    assert HookManager().judge(mixed, session_key="cron:job-1").tier != _TIER


def test_a_tool_the_declaration_no_longer_holds_asks_at_once(_world: dict[str, Any]) -> None:
    """The declared set is read at each call, so it follows ``app_agent_tools``' rule."""
    assert _judge(_call()).tier == _TIER
    assert _judge(_call("clock_added_later")).tier != _TIER
    _world["tools"] = [_declared(_OTHER_APP, _OTHER_TOOL)]
    assert _judge(_call()).tier != _TIER


@pytest.mark.parametrize("state", [False, None])
def test_an_app_that_is_not_readably_enabled_asks(_world: dict[str, Any], state: Any) -> None:
    _world["enabled"][_APP] = state
    assert _judge(_call()).tier != _TIER


def test_an_operator_confirm_pattern_and_the_read_only_mode_still_ask() -> None:
    assert _judge(_call(), confirm_tools=[f"@kirocrew-core/{_TOOL}"]).action == TOOL_ALLOW
    verdict = HookManager().judge(_call(), app=_APP, classifier_only=True)
    assert verdict.tier != _TIER


def test_a_deny_rule_on_the_tool_wins_over_the_grant() -> None:
    verdict = _judge(_call(), auto_deny_tools=[f"@kirocrew-core/{_TOOL}"])
    assert verdict.action == hooks.TOOL_DENY


def test_app_run_of_reads_the_host_stamp_and_the_client_agent() -> None:
    client = SimpleNamespace(_agent=_AGENT)
    stamped = SimpleNamespace(created_by=f"app:{_APP}", agent="default")
    assert own_agent_tools.app_run_of(stamped, client) == (_APP, _AGENT)
    for created_by in ("", "U12345", "app:", None):
        job = SimpleNamespace(created_by=created_by, agent=_AGENT)
        assert own_agent_tools.app_run_of(job, client)[0] == ""
    assert own_agent_tools.app_run_of(stamped, SimpleNamespace()) == (_APP, "")


class _OneRequest:
    """A provider whose turn asks for one tool call."""

    def __init__(self, event: LLMEvent) -> None:
        self._event = event
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        yield self._event
        yield LLMEvent(kind=EVENT_COMPLETE, text="")

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_run", "asked"),
    [((_APP, _AGENT), 0), (("", _AGENT), 1), ((_APP, "kirocrew"), 1), (("", ""), 1)],
)
async def test_an_unattended_turn_asks_unless_the_run_is_the_apps_own(
    app_run: tuple[str, str], asked: int
) -> None:
    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=f"Running: @kirocrew-core/{_TOOL}",
        request_id="r1",
    )
    event.mcp_server_name = "kirocrew-core"  # type: ignore[attr-defined]
    event.tool_name = _TOOL  # type: ignore[attr-defined]
    event.mcp_identity_trusted = True  # type: ignore[attr-defined]
    provider = _OneRequest(event)
    cards: list[str] = []

    async def card(request: LLMEvent) -> bool:
        cards.append(request.request_id)
        return False

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.HOOK_BASED,
        hooks=HookManager(),
        on_tool_approval=card,
        retry_transient=False,
        session_key="cron:job-1",
        app_run=app_run,
        unattended=True,
    )
    assert len(cards) == asked
    assert provider.approved == ([] if asked else ["r1"])
