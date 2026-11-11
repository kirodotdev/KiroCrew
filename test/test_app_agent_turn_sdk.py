"""``ctx.turns``: an app's agent turns, gated, admitted and scoped to its own sessions.

The SDK is the one public door to the gateway's session manager, its PreToolUse gate
and Shared Crew admission. What it fixes, and these tests pin, is what an app must
not choose: the identity the gate resolves a profile for, the approval policy, the
admission class, and which session keys it may address. Every turn first answers
the app's ``capabilities.spawn`` ceiling for the agent it runs. And an app that does
not declare ``permissions.agentTurns`` gets no SDK at all.

Every manager here is a recorder; no real session, provider or gate runs.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import llm_helpers
from kiro_crew.apps import agent_turn_sdk
from kiro_crew.apps.agent_turn_sdk import AgentTurnRefused, AgentTurnSDK
from kiro_crew.apps.context import build_app_context
from kiro_crew.platform import governance_profiles
from kiro_crew.shared_crew import turn_gate
from kiro_crew.shared_crew.admission import OperationKind

APP = "turn-probe"
#: Names a hang only; every awaited turn here finishes on its own signal.
_BOUND = 30.0


class _Provider:
    def __init__(self, ack: str = "acked") -> None:
        self.ack = ack
        self.cancels: list[float] = []

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> str:
        self.cancels.append(wait_ack_timeout)
        return self.ack


class _Manager:
    """Records every call the SDK makes on the session manager.

    Each ``get_or_create`` starts a new session, and ``get_agent`` answers the
    agent it was started with (``""`` for none), as the real manager records it.
    """

    def __init__(self, provider: _Provider | None = None) -> None:
        self.provider = provider or _Provider()
        self.events: list[tuple[str, ...]] = []
        self.live: set[str] = set()
        self.agents: dict[str, str] = {}

    async def get_or_create(self, key: str, agent: str | None = None, **kwargs: Any):
        self.events.append(("get_or_create", key, str(agent), *sorted(kwargs)))
        self.agents[key] = agent or ""
        return self.provider, True, False

    def get_agent(self, key: str) -> str:
        return self.agents.get(key, "")

    def release(self, key: str) -> None:
        self.events.append(("release", key))

    async def reset(self, key: str) -> None:
        self.events.append(("reset", key))

    async def destroy(self, key: str) -> None:
        self.events.append(("destroy", key))

    def has_session(self, key: str) -> bool:
        return key in self.live


@pytest.fixture(autouse=True)
def _host_default(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Today's default template is ``host-default``, never the host's own config."""
    _floor_monkeypatch.setattr(agent_turn_sdk, "_default_agent", lambda: "host-default")


@pytest.fixture
def streamed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every ``stream_and_collect`` call's keywords; each answers ``"reply"``."""
    calls: list[dict[str, Any]] = []

    async def _stream(provider: Any, message: str, **kwargs: Any) -> str:
        calls.append({"message": message, **kwargs})
        return "reply"

    monkeypatch.setattr(llm_helpers, "stream_and_collect", _stream)
    return calls


@pytest.fixture
def admitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every admission call's keywords; the work runs as admitted."""
    calls: list[dict[str, Any]] = []

    async def _admit(work: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return await work

    monkeypatch.setattr(turn_gate, "run_admitted_or_raise", _admit)
    return calls


def _sdk(manager: Any, hooks: Any = None) -> AgentTurnSDK:
    return AgentTurnSDK(APP, lambda: manager, lambda: hooks)


@pytest.mark.parametrize(
    "key",
    [
        "dashboard:chat-1-1",
        "other-app-session",
        APP,
        f"{APP}-",
        f"{APP}-a b",
        f"{APP}-../x",
        f"{APP}-a/b",
        f"{APP}-" + "x" * 401,
        f"x{APP}-a",
        None,
        42,
    ],
)
def test_a_key_outside_the_apps_prefix_is_refused_before_the_manager_is_touched(key):
    manager = _Manager()
    sdk = _sdk(manager)
    with pytest.raises(ValueError):
        asyncio.run(asyncio.wait_for(sdk.run(key, "hi"), _BOUND))
    with pytest.raises(ValueError):
        sdk.has_session(key)
    assert manager.events == []


def test_a_turn_carries_the_contexts_identity_and_the_hook_gate(streamed, admitted):
    manager = _Manager()
    hooks = object()
    reply = asyncio.run(
        asyncio.wait_for(_sdk(manager, hooks).run(f"{APP}-n-1", "a line", agent="a1"), _BOUND)
    )

    assert reply == "reply"
    assert len(streamed) == 1
    call = streamed[0]
    assert call["app"] == APP
    assert call["approval_policy"] is llm_helpers.ToolApprovalPolicy.HOOK_BASED
    assert call["hooks"] is hooks
    assert (call["session_key"], call["agent"], call["message"]) == (f"{APP}-n-1", "a1", "a line")
    assert admitted == [
        {
            "session_key": f"{APP}-n-1",
            "text": "a line",
            "automation": True,
            "operation": OperationKind.BACKGROUND,
        }
    ]
    # The session is kept for the next turn: released, never destroyed.
    assert manager.events == [
        ("get_or_create", f"{APP}-n-1", "a1", "owning_app"),
        ("release", f"{APP}-n-1"),
    ]


def test_no_method_lets_the_app_choose_its_identity_or_approval():
    for method in (AgentTurnSDK.run, AgentTurnSDK.run_oneshot):
        params = set(inspect.signature(method).parameters)
        assert not params & {"app", "approval_policy", "hooks", "automation", "operation"}


def test_an_empty_agent_starts_the_default_it_was_asked_as(monkeypatch, streamed, admitted):
    asked = _ceiling(monkeypatch, set())
    manager = _Manager()
    asyncio.run(asyncio.wait_for(_sdk(manager).run(f"{APP}-k", "hi"), _BOUND))

    # One name for all three: the ceiling's question, the session start, the gate.
    assert ("capabilities.spawn", "agents:host-default", APP) in asked
    assert manager.events[0] == ("get_or_create", f"{APP}-k", "host-default", "owning_app")
    assert streamed[0]["agent"] == "host-default"


def test_no_session_manager_is_a_failure_not_a_silent_skip():
    sdk = AgentTurnSDK(APP, lambda: None)
    with pytest.raises(RuntimeError, match="session manager unavailable"):
        asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi"), _BOUND))
    assert sdk.has_session(f"{APP}-k") is False


@pytest.mark.parametrize(("ack", "reset"), [("acked", False), ("no_turn", True)])
def test_a_turn_past_its_budget_is_cancelled_then_released(monkeypatch, admitted, ack, reset):
    started = asyncio.Event()

    async def _hang(provider: Any, message: str, **kwargs: Any) -> str:
        started.set()
        await asyncio.Event().wait()
        return ""

    monkeypatch.setattr(llm_helpers, "stream_and_collect", _hang)
    manager = _Manager(_Provider(ack))

    async def _go() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await _sdk(manager).run(
                f"{APP}-k",
                "hi",
                timeout_secs=0.01,  # flake-ok: the stream never ends; expiry is the only outcome
                cancel_ack_secs=0.25,
            )

    asyncio.run(asyncio.wait_for(_go(), _BOUND))
    assert manager.provider.cancels == [0.25]
    expected = [("get_or_create", f"{APP}-k", "host-default", "owning_app")]
    if reset:
        expected.append(("reset", f"{APP}-k"))
    assert manager.events == [*expected, ("release", f"{APP}-k")]


def test_a_oneshot_turn_rejects_every_tool_and_leaves_no_session(streamed, admitted):
    manager = _Manager()
    reply = asyncio.run(
        asyncio.wait_for(_sdk(manager).run_oneshot("translate this", label="translate"), _BOUND)
    )

    assert reply == "reply"
    call = streamed[0]
    assert call["approval_policy"] is llm_helpers.ToolApprovalPolicy.REJECT_ALL
    key = admitted[0]["session_key"]
    assert key.startswith(f"{APP}-translate-") and len(key) == len(f"{APP}-translate-") + 32
    assert admitted[0]["automation"] is True
    assert admitted[0]["operation"] is OperationKind.BACKGROUND
    assert manager.events == [
        ("get_or_create", key, "kirocrew-lite"),
        ("release", key),
        ("destroy", key),
    ]


@pytest.mark.parametrize("label", ["", "Upper", "-lead", "a b", "x" * 33, "a/b", None])
def test_a_oneshot_label_outside_the_vocabulary_is_refused(label):
    manager = _Manager()
    with pytest.raises(ValueError):
        asyncio.run(asyncio.wait_for(_sdk(manager).run_oneshot("p", label=label), _BOUND))
    assert manager.events == []


def test_the_managers_are_read_off_the_application_on_each_call(streamed, admitted):
    manager = _Manager()
    manager.live.add(f"{APP}-k")
    http_app: dict[str, Any] = {}
    sdk = AgentTurnSDK.for_http_app(APP, http_app)
    assert sdk.has_session(f"{APP}-k") is False

    hooks = object()
    http_app["state"] = SimpleNamespace(
        sessions=manager, context_builder=SimpleNamespace(hooks=hooks)
    )
    assert sdk.has_session(f"{APP}-k") is True
    asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi"), _BOUND))
    assert streamed[0]["hooks"] is hooks


def test_no_session_open_claims_a_start_priority():
    tree = ast.parse(Path(agent_turn_sdk.__file__).read_text(encoding="utf-8"))
    opens = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_or_create"
    ]
    assert len(opens) == 2
    assert [kw.arg for call in opens for kw in call.keywords if kw.arg == "start_priority"] == []


def test_an_app_that_does_not_declare_agent_turns_gets_no_sdk(tmp_path: Path):
    host = {"state": SimpleNamespace(sessions=_Manager())}

    def _ctx(permissions: dict[str, Any], turn_host: Any = host):
        return build_app_context(APP, tmp_path, permissions=permissions, turn_host=turn_host)

    assert _ctx({}).turns is None
    assert _ctx({"spawn": True, "storage": True}).turns is None
    # A grant must be the literal `true`: the string "true" grants nothing.
    assert _ctx({"agentTurns": "true"}).turns is None
    assert _ctx({"agentTurns": 1}).turns is None
    # Declared, but no gateway Application was wired in.
    assert _ctx({"agentTurns": True}, turn_host=None).turns is None

    granted = _ctx({"agentTurns": True}).turns
    assert isinstance(granted, AgentTurnSDK)
    assert granted.app_name == APP
    assert granted.has_session(f"{APP}-k") is False
    with pytest.raises(ValueError):
        granted.has_session("other-k")


def test_the_manifest_round_trips_the_grant_only_as_a_literal_true():
    from kiro_crew.apps.manifest_sections import Permissions

    assert Permissions.from_dict({"agentTurns": True}).agentTurns is True
    assert Permissions.from_dict({"agentTurns": True}).to_dict() == {"agentTurns": True}
    assert Permissions.from_dict({"agentTurns": "true"}).agentTurns is False
    assert "agentTurns" not in Permissions.from_dict({}).to_dict()


# ── the spawn ceiling ───────────────────────────────────────────────────────


def _ceiling(monkeypatch: pytest.MonkeyPatch, denied: set[str]) -> list[tuple[str, str, str]]:
    """Answer ``governance_permits`` from *denied*; record every question asked.

    *denied* holds ``""`` (spawning off) or ``"agents:<name>"`` items. The app name
    the question carries is recorded, so a test can tell it reached the profile.
    """
    asked: list[tuple[str, str, str]] = []

    def _permits(scope: str, item: str = "", **kwargs: Any) -> Any:
        asked.append((scope, item, kwargs.get("app", "")))
        return SimpleNamespace(permitted=item not in denied, reason=f"denies {item or 'spawn'}")

    monkeypatch.setattr(governance_profiles, "governance_permits", _permits)
    return asked


def test_control_a_permitted_named_agent_asks_both_questions_then_runs(
    monkeypatch, streamed, admitted
):
    asked = _ceiling(monkeypatch, set())
    manager = _Manager()
    reply = asyncio.run(asyncio.wait_for(_sdk(manager).run(f"{APP}-k", "hi", agent="a1"), _BOUND))

    assert reply == "reply"
    assert asked == [("capabilities.spawn", "", APP), ("capabilities.spawn", "agents:a1", APP)]
    assert manager.events[0] == ("get_or_create", f"{APP}-k", "a1", "owning_app")


def test_a_disabled_spawn_ceiling_refuses_every_turn_before_a_session_opens(
    monkeypatch, streamed, admitted
):
    _ceiling(monkeypatch, {""})
    manager = _Manager()
    sdk = _sdk(manager)
    with pytest.raises(AgentTurnRefused, match="spawn policy"):
        asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi", agent="a1"), _BOUND))
    with pytest.raises(AgentTurnRefused):
        asyncio.run(asyncio.wait_for(sdk.run_oneshot("p", label="translate"), _BOUND))
    assert manager.events == []
    assert streamed == [] and admitted == []


def test_the_agents_scope_refuses_only_the_agent_outside_it(monkeypatch, streamed, admitted):
    _ceiling(monkeypatch, {"agents:a1"})
    manager = _Manager()
    sdk = _sdk(manager)
    with pytest.raises(AgentTurnRefused):
        asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi", agent="a1"), _BOUND))
    assert manager.events == []

    asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi", agent="a2"), _BOUND))
    assert manager.events[0] == ("get_or_create", f"{APP}-k", "a2", "owning_app")


def test_an_empty_agent_is_asked_as_the_default_it_starts_as(monkeypatch, streamed, admitted):
    asked = _ceiling(monkeypatch, {"agents:host-default"})
    monkeypatch.setattr(agent_turn_sdk, "_default_agent", lambda: "host-default")
    manager = _Manager()
    with pytest.raises(AgentTurnRefused):
        asyncio.run(asyncio.wait_for(_sdk(manager).run(f"{APP}-k", "hi"), _BOUND))
    assert ("capabilities.spawn", "agents:host-default", APP) in asked
    assert manager.events == []


@pytest.mark.parametrize("default", ["", None])
def test_an_unresolvable_default_agent_is_refused(monkeypatch, streamed, admitted, default):
    _ceiling(monkeypatch, set())

    def _unresolved() -> str:
        if default is None:
            raise OSError("config unreadable")
        return default

    monkeypatch.setattr(agent_turn_sdk, "_default_agent", _unresolved)
    manager = _Manager()
    with pytest.raises(AgentTurnRefused, match="default agent"):
        asyncio.run(asyncio.wait_for(_sdk(manager).run(f"{APP}-k", "hi"), _BOUND))
    assert manager.events == []


def test_a_oneshot_turn_is_asked_about_its_fixed_agent(monkeypatch, streamed, admitted):
    asked = _ceiling(monkeypatch, {"agents:kirocrew-lite"})
    manager = _Manager()
    with pytest.raises(AgentTurnRefused):
        asyncio.run(asyncio.wait_for(_sdk(manager).run_oneshot("p", label="translate"), _BOUND))
    assert ("capabilities.spawn", "agents:kirocrew-lite", APP) in asked
    assert manager.events == []


# ── a reused session is asked about the agent it runs ──────────────────────


class _LiveManager(_Manager):
    """A manager that keeps each session: the second turn on a key reuses the first."""

    async def get_or_create(self, key: str, agent: str | None = None, **kwargs: Any):
        self.events.append(("get_or_create", key, str(agent), *sorted(kwargs)))
        if key in self.live:
            return self.provider, False, False
        self.live.add(key)
        self.agents[key] = agent or ""
        return self.provider, True, False


def test_control_a_reused_session_whose_agent_is_still_permitted_runs(
    monkeypatch, streamed, admitted
):
    _ceiling(monkeypatch, set())
    monkeypatch.setattr(agent_turn_sdk, "_default_agent", lambda: "default-a")
    manager = _LiveManager()
    sdk = _sdk(manager)
    for _ in range(2):
        asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi"), _BOUND))
    assert len(streamed) == 2


def test_a_reused_default_session_is_asked_as_the_default_it_started_as(
    monkeypatch, streamed, admitted
):
    asked = _ceiling(monkeypatch, set())
    default = {"name": "default-a"}
    monkeypatch.setattr(agent_turn_sdk, "_default_agent", lambda: default["name"])
    manager = _LiveManager()
    sdk = _sdk(manager)
    asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi"), _BOUND))

    # The operator moves the default to B and narrows the app to B only.
    default["name"] = "default-b"
    _ceiling(monkeypatch, {"agents:default-a"})
    with pytest.raises(AgentTurnRefused, match="spawn policy"):
        asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi"), _BOUND))

    assert ("capabilities.spawn", "agents:default-a", APP) in asked
    assert len(streamed) == 1
    # The refused turn still hands back the lease it took.
    assert manager.events[-1] == ("release", f"{APP}-k")


def test_a_reused_session_is_asked_about_its_own_agent_not_the_requested_one(
    monkeypatch, streamed, admitted
):
    _ceiling(monkeypatch, set())
    manager = _LiveManager()
    sdk = _sdk(manager)
    asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi", agent="a1"), _BOUND))

    _ceiling(monkeypatch, {"agents:a1"})
    with pytest.raises(AgentTurnRefused):
        asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi", agent="a2"), _BOUND))
    assert len(streamed) == 1
    assert manager.events[-1] == ("release", f"{APP}-k")


def test_a_reused_session_whose_agent_nothing_records_is_refused(monkeypatch, streamed, admitted):
    _ceiling(monkeypatch, set())
    monkeypatch.setattr(agent_turn_sdk, "_default_agent", lambda: "default-a")
    manager = _LiveManager()
    manager.live.add(f"{APP}-k")  # started by something other than this SDK
    with pytest.raises(AgentTurnRefused, match="not recorded"):
        asyncio.run(asyncio.wait_for(_sdk(manager).run(f"{APP}-k", "hi"), _BOUND))
    assert streamed == []
    assert manager.events[-1] == ("release", f"{APP}-k")


# ── the hook gate judges the agent the session runs ────────────────────────


def test_a_reused_session_hands_the_gate_its_own_agent_not_the_requested_one(
    monkeypatch, streamed, admitted
):
    _ceiling(monkeypatch, set())
    manager = _LiveManager()
    sdk = _sdk(manager)
    asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi", agent="a1"), _BOUND))
    # Both agents are permitted, so only the gate's agent can tell them apart.
    asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi", agent="a2"), _BOUND))

    assert [call["agent"] for call in streamed] == ["a1", "a1"]


def test_a_default_session_hands_the_gate_the_default_it_started_as(
    monkeypatch, streamed, admitted
):
    _ceiling(monkeypatch, set())
    default = {"name": "default-a"}
    monkeypatch.setattr(agent_turn_sdk, "_default_agent", lambda: default["name"])
    manager = _LiveManager()
    sdk = _sdk(manager)
    asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi"), _BOUND))
    default["name"] = "default-b"
    asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi", agent="a2"), _BOUND))

    assert [call["agent"] for call in streamed] == ["default-a", "default-a"]


def test_a_session_whose_record_names_no_agent_is_refused_on_every_path(
    monkeypatch, streamed, admitted
):
    _ceiling(monkeypatch, set())

    class _Unrecorded(_Manager):
        def get_agent(self, key: str) -> str:
            return ""

    manager = _Unrecorded()
    sdk = _sdk(manager)
    with pytest.raises(AgentTurnRefused, match="not recorded"):
        asyncio.run(asyncio.wait_for(sdk.run(f"{APP}-k", "hi", agent="a1"), _BOUND))
    with pytest.raises(AgentTurnRefused, match="not recorded"):
        asyncio.run(asyncio.wait_for(sdk.run_oneshot("p", label="translate"), _BOUND))

    assert streamed == []
    oneshot_key = admitted[0]["session_key"]
    assert manager.events == [
        ("get_or_create", f"{APP}-k", "a1", "owning_app"),
        ("release", f"{APP}-k"),
        ("get_or_create", oneshot_key, "kirocrew-lite"),
        ("release", oneshot_key),
        ("destroy", oneshot_key),
    ]


def test_a_person_only_directive_the_turn_calls_is_refused_out_loud(monkeypatch, admitted):
    """No session applier reads an app turn: ``set_project`` is refused and audited."""
    from kiro_crew import session_directive
    from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_RESULT, AcpEvent

    audits: list[dict[str, Any]] = []

    class _SelSpy:
        def log_tool_invocation(self, **kw: Any) -> None:
            audits.append(kw)

    async def _stream(provider: Any, message: str, *, on_event: Any = None, **_kw: Any) -> str:
        if on_event is not None:
            await on_event(
                AcpEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id="tc-1",
                    tool_name="set_project",
                    mcp_server_name=session_directive.CORE_MCP_SERVER,
                )
            )
            await on_event(AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", tool_final=True))
        return "Project change requested for this session."

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _SelSpy())
    monkeypatch.setattr(llm_helpers, "stream_and_collect", _stream)
    key = f"{APP}-n-1"

    reply = asyncio.run(asyncio.wait_for(_sdk(_Manager()).run(key, "a line"), _BOUND))

    assert reply.startswith("Error: set_project only works from a user-facing session"), reply
    assert f"(this turn is {key!r})" in reply
    assert reply.endswith("\n\nProject change requested for this session.")
    assert [
        (a["session_key"], a["tool_name"], a["outcome"])
        for a in audits
        if a.get("source") == "mcp-directive"
    ] == [(key, "set_project", "denied")]
