"""A sub-agent's per-run scratch directory is never passed on as its project.

A run spawned without a cwd records its provider's per-run directory
(``_session_work_dir("subagent:<id>")``) as its cwd, and a continuation restores
that recorded path into ``info.cwd``. ``run_turn`` must still build the turn
with no project, while a real checkout given as cwd stays the project.
"""

from __future__ import annotations

import asyncio

import pytest
from test_subagent_skill_trigger_text import _RUN_CEILING, _mock_sessions  # noqa: F401

from kiro_crew.config.loader_locations import _session_work_dir, workspace_root

pytestmark = pytest.mark.usefixtures("healthy_host_memory", "ample_host_resources")

#: A run id in the shape ``_mint_agent_id`` draws.
_RUN_ID = "0123456789abcdef"


def _marked_run_dir(run_id: str):
    from kiro_crew.session_work_dir import mark_run_dir

    scratch = _session_work_dir(f"subagent:{run_id}")
    assert mark_run_dir(scratch)
    return scratch


def test_only_a_marked_subagent_work_dir_is_recognised(tmp_path):
    from kiro_crew.session_work_dir import is_subagent_work_dir

    for run_id in (_RUN_ID, "0123abcd"):
        scratch = _marked_run_dir(run_id)
        assert is_subagent_work_dir(scratch)
        assert is_subagent_work_dir(str(scratch))
    # The same name with no marker is a person's directory, not a run's.
    unmarked = _session_work_dir("subagent:fedcba9876543210")
    unmarked.mkdir(parents=True)
    assert not is_subagent_work_dir(unmarked)
    # A chat slot's work dir, the root itself, a lookalike name under the
    # root and the run's name elsewhere are not either.
    assert not is_subagent_work_dir(_session_work_dir("dashboard:chat-1"))
    assert not is_subagent_work_dir(workspace_root())
    lookalike = workspace_root() / "subagent_project"
    lookalike.mkdir(parents=True, exist_ok=True)
    assert not is_subagent_work_dir(lookalike)
    assert not is_subagent_work_dir(tmp_path / f"subagent_{_RUN_ID}")


async def _child_build_kwargs(cwd: str) -> dict:
    from unittest.mock import MagicMock, patch

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.subagent import SubagentManager

    # An explicit cwd is admitted only under a configured root.
    cfg = KiroCrewConfig.load()
    cfg.agent.subagent_cwd_allowed_roots = [str(workspace_root())]
    cfg.save()

    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=ctx)
    mgr._should_use_session_sharing = MagicMock(return_value=False)  # type: ignore[method-assign]
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("summarize the notes", cwd=cwd)
        assert info is not None
        assert info.id in mgr._tasks, (info.cwd, getattr(info, "result", None))
        await asyncio.wait_for(mgr._tasks[info.id], _RUN_CEILING)
    assert ctx.build_message.called, "build_message was never reached"
    return ctx.build_message.call_args.kwargs


@pytest.mark.asyncio
async def test_a_recorded_scratch_cwd_builds_the_turn_with_no_project():
    scratch = _marked_run_dir(_RUN_ID)
    kwargs = await _child_build_kwargs(str(scratch))
    assert kwargs["project"] is None
    assert kwargs["template_dir"] == str(scratch)


@pytest.mark.asyncio
async def test_an_unmarked_cwd_with_a_run_dir_name_stays_the_project():
    checkout = _session_work_dir(f"subagent:{_RUN_ID}")
    checkout.mkdir(parents=True)
    kwargs = await _child_build_kwargs(str(checkout))
    assert kwargs["project"] == str(checkout)
