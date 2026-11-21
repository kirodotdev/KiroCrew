"""The approval each sub-agent path asks for, pinned for both values of ``agent.subagents``.

Kiro Crew's own path (``spawn_sub_agents`` -> the sub-agent manager) raises one
approval per child unless the session is trusted, YOLO is on or
``hooks.auto_approve_subagent_spawn`` is set. The engine's path raises one
permission request per dispatch, which Kiro Crew's gate keeps at ask under the
default hooks AND with ``auto_approve_subagent_spawn`` set: that switch trusts
``spawn_run`` and the manager's children only, so on the engine path it adds no
trust. Nothing here auto-approves the shipped helper.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from test_kas_subagent_permission import _event, _subagent_params

from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, HookManager, HooksConfig, hook_gate_kwargs

pytestmark = pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")


def _helper_dispatch():
    params = _subagent_params()
    params["_meta"]["kiro"]["consent"]["resource"] = "kirocrew-helper"
    params["toolCall"]["title"] = "Sub-agent: kirocrew-helper"
    return _event(params)


@pytest.mark.parametrize(
    "hooks",
    [HooksConfig(), HooksConfig(auto_approve_subagent_spawn=True)],
    ids=["default-hooks", "auto-approve-subagent-spawn"],
)
def test_an_engine_dispatch_of_the_helper_asks(hooks):
    event = _helper_dispatch()
    verdict = HookManager(hooks).on_tool_call(event.title, **hook_gate_kwargs(event))
    assert verdict.action not in (TOOL_AUTO_APPROVE, TOOL_DENY)


@pytest.mark.asyncio
async def test_each_crew_sub_agent_asks_once_under_the_default_settings():
    """Three children, three approvals (``spawn_sub_agents`` spawns each this way)."""
    from kiro_crew.subagent import SubagentManager

    asked: list[str] = []

    async def approve(request_id: str, description: str, parent_session_key: str = "") -> bool:
        asked.append(request_id)
        return False

    for i in range(3):
        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        ctx_builder = MagicMock()
        ctx_builder.hooks = HooksConfig()
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=ctx_builder,
            on_spawn_approval=approve,
            is_yolo=lambda: False,
        )
        info = manager.spawn(f"task {i}", parent_session_key="dashboard:t1")
        assert info is not None and info.id in manager._tasks
        await manager._tasks[info.id]
        assert len(asked) == i + 1
