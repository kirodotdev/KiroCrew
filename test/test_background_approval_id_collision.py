"""Two background runtimes asking with the same request id each get their own approval.

A backend's ``session/request_permission`` id is a JSON-RPC id, counted per
connection: two sub-agents in their own runtimes both send ``1`` for their first
tool call. The gateway's background approvals share one registry, so the id the
dashboard registers must name ONE request. Each card the feed shows resolves the
request it was raised for, and neither request is left waiting with no card.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from chat_test_helpers import _make_state

from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.testing.wait import async_wait_until, finished


def _gateway(state):
    # No Slack: the callback takes the dashboard-only path, as on a dashboard host.
    return SimpleNamespace(
        slack=None,
        _owner_id="",
        dashboard_state=state,
        _cfg=SimpleNamespace(hooks={}),
        _approval_mode=None,
        autonudge_svc=None,
        sessions=None,
    )


@pytest.mark.asyncio
async def test_same_request_id_from_two_children_gets_two_live_approvals(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    frames: list[dict] = []
    monkeypatch.setattr(
        state, "broadcast_ws", lambda kind, data: frames.append({"kind": kind, **data})
    )
    approve = GatewayOrchestrator._interactive_approval(_gateway(state), "subagent")

    def ask(title: str) -> asyncio.Task[bool]:
        event = LLMEvent(kind="permission_request", request_id="1", title=title)
        return asyncio.create_task(approve(event, ""))

    first = ask("date > /tmp/a.txt")
    second = ask("date > /tmp/b.txt")
    await async_wait_until(
        lambda: len([f for f in frames if f["kind"] == "approval"]) == 2,
        describe=lambda: f"approval frames: {frames!r}",
    )
    cards = [f for f in frames if f["kind"] == "approval"]

    # Both requests stay listed, each under its own id.
    assert len({c["id"] for c in cards}) == 2
    assert sorted(r["tool"] for r in state._pending_approvals.values()) == [
        "date > /tmp/a.txt",
        "date > /tmp/b.txt",
    ]

    # The feed's Approve resolves by the id its card carries; each press lands.
    for card in cards:
        assert state.resolve_approval(card["id"], True) is True
    assert await finished(first, "first child's approval") is True
    assert await finished(second, "second child's approval") is True
    assert state._pending_approvals == {}


@pytest.mark.asyncio
async def test_spawn_approval_keeps_its_agent_id(tmp_path, monkeypatch):
    """A spawn id already names one agent; the spawn card and its resolver read it."""
    state = _make_state(tmp_path)
    frames: list[dict] = []
    monkeypatch.setattr(
        state, "broadcast_ws", lambda kind, data: frames.append({"kind": kind, **data})
    )
    approve = GatewayOrchestrator._interactive_approval(_gateway(state), "subagent")
    task = asyncio.create_task(
        approve(
            LLMEvent(kind="permission_request", request_id="spawn:a1", title="spawn_run(t)"), ""
        )
    )
    await async_wait_until(lambda: "spawn:a1" in state._pending_approvals)

    assert state.resolve_approval("spawn:a1", True) is True
    assert await finished(task, "spawn approval") is True
    assert [f["id"] for f in frames if f["kind"] == "approval"] == ["spawn:a1"]
