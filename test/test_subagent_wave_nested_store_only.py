"""A wave-close digest does not claim completion over a store-only nested spawn.

A wave member that calls ``spawn_run`` while the dispatch window is full gets
its child accepted only as a task-store row: no ``_queue`` entry and no
``_agents`` record. When the wave's last member reports, the digest must still
see that child as outstanding nested work, exactly as it does for a child that
sits in the window, and must not tell the parent "This run is complete".
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_scale_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _close_subagent_managers,
    _drain_injections,
    _make_orchestrator,
    _mock_ctx,
    _mock_dashboard_state,
    _mock_sessions,
)

from kiro_crew import taskq
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.testing.wait import async_wait_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

WAVE = "bigwave"
TOTAL = 2
NESTED_PARENT = "subagent:w1"


def _member(i: int) -> SubagentInfo:
    info = SubagentInfo(
        id=f"w{i}",
        task=f"wave task {i}",
        parent_session_key="dashboard:main",
        batch_id=WAVE,
        batch_total=TOTAL,
    )
    info.done = True
    info.result = f"result {i}"
    info.result_path = f"/tmp/w{i}/result.txt"
    info.elapsed = 10.0 + i
    info.credits = 0.25 + i
    return info


async def _manager(*, nested: bool) -> tuple[SubagentManager, list[SubagentInfo]]:
    """A real manager holding the wave's two finished members.

    One run slot and a one-entry window: the first spawn starts and the second
    fills the window, so with *nested* the child member ``w1`` spawned is
    accepted only in the store.
    """
    mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), max_concurrent=1)
    await mgr.wait_taskq_ready()
    mgr._spawn_stagger_secs = 0.0
    store = mgr._taskq
    assert store is not None
    store._window = 1
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch.object(SubagentManager, "_run", new=AsyncMock()),
    ):
        mgr.spawn("blocker", parent_session_key="dashboard:other")
        mgr.spawn("filler", parent_session_key="dashboard:other")
        if nested:
            mgr.spawn(
                "nested child",
                parent_session_key=NESTED_PARENT,
                batch_id="nestwave",
                batch_total=1,
            )
    members = [_member(i) for i in range(TOTAL)]
    for member in members:
        mgr._agents[member.id] = member
    # Fixture: the child is in neither the window nor the registry, only the store.
    assert not any(p.get("parent_session_key") == NESTED_PARENT for p in mgr._queue)
    assert not any(a.parent_session_key == NESTED_PARENT for a in mgr._agents.values())
    pending = store.count_pending(taskq.KIND_SUBAGENT, session_key=NESTED_PARENT)
    assert pending == (1 if nested else 0)
    return mgr, members


async def _wave_close_digest(*, nested: bool) -> str:
    """Report both members through the gateway's completion consumer and
    return what it injected into the parent's tab."""
    real, members = await _manager(nested=nested)
    orch = _make_orchestrator()
    orch.sessions = _mock_sessions()
    orch.ctx_builder = MagicMock()
    orch.ctx_builder.hooks = MagicMock()
    orch.dashboard_state = _mock_dashboard_state()
    slot = MagicMock()
    slot.mode = "chat"
    slot.running = False
    slot.task = None
    slot._subagent_deliveries_inflight = 0
    orch.dashboard_state.get_slot = MagicMock(return_value=slot)
    with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
        with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
            mgr = MagicMock()
            mgr.start_reaper = MagicMock()
            mock_sm.return_value = mgr
            orch._init_subagents()
            orch.subagent_mgr = mgr
            on_done = mock_sm.call_args.kwargs["on_done"]
    # The digest's nested-work reads go to the real manager, which holds the
    # wave members and the store-only child.
    mgr.wave_has_live_nested_spawns = real.wave_has_live_nested_spawns
    mgr.wave_has_live_nested_spawns_async = getattr(real, "wave_has_live_nested_spawns_async", None)
    mgr.batch_members_pending = MagicMock(return_value=False)
    mgr.batch_reports_in_flight = MagicMock(return_value=False)
    injected: list[str] = []

    async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
        injected.append(text)

    with (
        patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat),
        patch("kiro_crew.subagent_persistence.mark_delivered"),
    ):
        for member in members:
            await on_done(member)
            await asyncio.sleep(0)
        await async_wait_until(
            lambda: len(injected) >= 1,
            timeout=5.0,
            describe=lambda: "the wave-close digest injected",
        )
        await _drain_injections(orch)
    return "\n".join(injected)


@pytest.mark.asyncio
async def test_wave_close_digest_sees_a_store_only_nested_spawn():
    body = await _wave_close_digest(nested=True)
    assert "This run is complete" not in body
    assert "All results delivered" not in body
    assert "2 sub-agents finished" in body
    assert "nested work" in body


@pytest.mark.asyncio
async def test_wave_close_digest_still_completes_without_nested_work():
    """The store read does not change the wording when no member has a
    queued child."""
    body = await _wave_close_digest(nested=False)
    assert "This run is complete" in body
    assert "nested work" not in body
