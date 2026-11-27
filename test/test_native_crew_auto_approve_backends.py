"""The native-crew auto-approve rung runs only on backends that keep a child's requests apart.

``dashboard.chat_turn.tool_approval`` approves a request with no card while a native
sub-agent's card is open and ``hooks.auto_approve_subagent_tools`` is on, only for a
request the transport marks as the sub-agent's, and only on kiro-cli and KAS
(``SessionCapabilities.permission_requests_name_child``). On any other backend, a
backend this build does not know included, and on kiro-cli and KAS too, the parent's
own call keeps its card.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from turn_harness import REJECTED, Do, ScriptedProvider, SlotSpec, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_SUBAGENT_LIST,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_PI,
    ACP_BACKENDS_KNOWN,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.hook_runtime.hook_types import HooksConfig
from kiro_crew.hooks import HookManager

CHILD = "child-0001"

#: Backends that do not show a child's requests apart, and one nobody registered.
NOT_TAGGED = (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_PI,
    ACP_BACKEND_DEEPSEEK,
    "some-harness-nobody-registered",
)
TAGGED = (ACP_BACKEND_KIRO, ACP_BACKEND_KAS)

#: How each tagged transport marks a child's request: a kiro-cli child's frame names
#: its own session (with its structured params, or it is a low-fidelity child call);
#: a native KAS child shares the parent's and is told apart by its tool-call id.
CHILD_MARKS = {
    ACP_BACKEND_KIRO: {
        "sub_session_id": CHILD,
        "child_calls_marked": True,
        "raw_tool_params": {"command": "touch out.txt"},
        "raw_params_trusted": True,
        "shell_classified": True,
    },
    ACP_BACKEND_KAS: {
        "native_child_call": True,
        "tool_call_id": "tc-1",
        "child_calls_marked": True,
    },
}


def _provider(backend: str) -> type[ScriptedProvider]:
    return type("_Provider", (ScriptedProvider,), {"capabilities": capabilities_for(backend)})


def _hook_on(ctx) -> None:
    ctx.state.context_builder = SimpleNamespace(
        hooks=HookManager(HooksConfig(auto_approve_subagent_tools=True))
    )


def _hook_off(ctx) -> None:
    ctx.state.context_builder = None


def _card() -> AcpEvent:
    entry = {
        "sessionId": CHILD,
        "agentName": "general",
        "initialQuery": "probe child",
        "status": {"type": "working", "message": ""},
    }
    return AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=[entry])


def _shell_request(**marks) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id="r1",
        title="touch out.txt",
        tool_name="shell",
        tool_kind="execute",
        is_shell=True,
        tool_input=json.dumps({"command": "touch out.txt"}),
        **marks,
    )


async def _run(backend: str, *, hook: bool = True, child: bool = False, **slot):
    done = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
    request = _shell_request(**(CHILD_MARKS[backend] if child else {}))
    steps = [Do(_hook_on if hook else _hook_off), _card(), request]
    return await run_turn(
        TurnScript(
            events=[*steps, Do(_hook_off), done],
            answers={"r1": REJECTED},
            provider=_provider(backend),
        ),
        slot=SlotSpec(**slot),
    )


def test_only_kiro_and_kas_say_their_requests_name_the_child() -> None:
    """claude and opencode draw native cards too, and their requests name no child."""
    tagged = {b for b in ACP_BACKENDS_KNOWN if capabilities_for(b).permission_requests_name_child}
    assert tagged == set(TAGGED)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", (*NOT_TAGGED, *TAGGED))
async def test_an_open_card_leaves_the_parents_request_on_a_card(backend: str) -> None:
    record = await _run(backend)
    assert len(record.approval_cards) == 1
    assert record.calls("approve_tool") == []
    assert record.audits(request_id="r1", outcome="auto_approved") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", TAGGED)
async def test_an_open_card_still_auto_approves_a_childs_request_on_kiro_and_kas(
    backend: str,
) -> None:
    record = await _run(backend, child=True)
    assert record.approval_cards == []
    assert len(record.calls("approve_tool")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", TAGGED)
async def test_kiro_and_kas_ask_with_the_hook_off(backend: str) -> None:
    record = await _run(backend, hook=False, child=True)
    assert len(record.approval_cards) == 1
    assert record.calls("approve_tool") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", NOT_TAGGED[:2])
@pytest.mark.parametrize("posture", [{"trust": True}, {"yolo": True}])
async def test_trust_and_yolo_still_approve_through_their_own_rung(backend, posture) -> None:
    """The change only closes the crew rung; a trusted or YOLO slot approves as before."""
    record = await _run(backend, **posture)
    assert record.approval_cards == []
    assert len(record.calls("approve_tool")) == 1
