"""The interactive approval floor on the subagent ladder (remote-placed runs)."""

from __future__ import annotations

import asyncio

import pytest
from test_tool_permission import (  # noqa: F401 - names is a fixture, used by name
    RecordingWire,
    _event,
    _low_fidelity_event,
    _subagent_ladder,
    names,
)

from kiro_crew.agent_sdk.spec_hooks import TurnSpecHooks
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.subagent_manager import run as run_mod
from kiro_crew.testing.wait import async_wait_until, await_until
from kiro_crew.tool_permission import Ask, settle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hook", "low", "attended", "answer", "rung"),
    [
        # The gate's own grant (auto_approve_tools=["*"]) does not stand in for
        # the hub's person, at full fidelity or for a low-fidelity child.
        (TOOL_AUTO_APPROVE, False, False, True, "hub"),
        (TOOL_AUTO_APPROVE, True, False, True, "hub"),
        # A person on this gateway is not the floor's person: only the hub is asked.
        (TOOL_AUTO_APPROVE, False, True, True, "hub"),
        (TOOL_ALLOW, False, True, False, "hub"),
        # A deny still refuses before anyone is asked.
        (TOOL_DENY, False, True, None, "hook_deny"),
    ],
)
@pytest.mark.usefixtures("names")
async def test_an_interactive_floor_hands_every_request_to_the_hub(
    hook, low, attended, answer, rung
):
    from kiro_crew.subagent_manager import hub_approvals

    asked: list = []

    async def callback(event, parent_session_key=""):
        asked.append(event.approval_floor)
        return True

    policy, _info, log = _subagent_ladder(
        hook=hook, callback=callback if attended else None, approval_floor="interactive"
    )
    event = _low_fidelity_event(identity_verified=True) if low else _event()
    pending = asyncio.create_task(settle(Ask(event, RecordingWire(log), "subagent:a1"), policy))
    if answer is not None:
        listed = await async_wait_until(
            lambda: hub_approvals.pending_for("a1"),
            describe=lambda: "no hub approval listed for a1",
        )
        assert len(listed) == 1 and listed[0]["title"] == event.title
        assert hub_approvals.resolve("a1", listed[0]["id"], answer)
    settled = await await_until(pending, what="the floored ask")
    assert settled.rung == rung
    if answer is not None:
        assert settled.outcome == ("approved" if answer else "rejected")
    assert asked == []
    assert hub_approvals.pending_for("a1") == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("names")
async def test_a_floored_runs_template_hooks_refuse_before_the_hub_is_asked():
    from kiro_crew.subagent_manager import hub_approvals

    # Hooks that cannot be read give no verdict, so they refuse like a deny.
    policy, _info, log = _subagent_ladder(
        spec=TurnSpecHooks([], None, True, True), approval_floor="interactive"
    )
    settled = await await_until(
        asyncio.ensure_future(settle(Ask(_event(), RecordingWire(log), "subagent:a1"), policy)),
        what="the hook refusal",
    )
    assert settled.rung == "spec_hook"
    assert hub_approvals.pending_for("a1") == []


@pytest.mark.asyncio
async def test_without_a_floor_the_callback_sees_no_floor():
    asked: list = []

    async def callback(event, parent_session_key=""):
        asked.append(event.approval_floor)
        return True

    policy, _info, log = _subagent_ladder(callback=callback)
    settled = await settle(Ask(_event(), RecordingWire(log), "subagent:a1"), policy)
    assert settled.rung == "callback" and asked == [""]


@pytest.mark.asyncio
@pytest.mark.parametrize(("floor", "aliases"), [("interactive", True), ("", None)])
async def test_only_a_floored_ladder_asks_its_spec_hooks_for_kiro_cli_aliases(
    monkeypatch, floor, aliases
):
    seen: list = []

    async def pre_tool(*_a, **kw):
        seen.append(kw.get("native_aliases"))
        return None

    policy, _info, _log = _subagent_ladder(
        spec=TurnSpecHooks(["hook"], "/work", False, True), approval_floor=floor
    )
    monkeypatch.setattr(run_mod, "permission_pre_tool_block", pre_tool)
    assert await policy.floors[0].refuse(Ask(_event(), RecordingWire([]), "k")) is None
    assert seen == [aliases]
