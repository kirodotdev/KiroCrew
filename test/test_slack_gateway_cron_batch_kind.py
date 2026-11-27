"""A cron fire on a ``cron:`` batch a person's turn projected.

A scheduled fire re-projects that batch first, deferring while work is pending on
its key or when the re-projection does not land; an ``agent_sequence`` decides at
its sweep, so no completed step replays; a manual Run never defers for the kind.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _make_orchestrator,
    _mock_context_builder,
    _mock_dashboard_state,
    _mock_sessions,
)


def _job(agent_sequence: list[str]) -> MagicMock:
    job = MagicMock()
    job.execution_context = None
    job.member_id = ""
    job.memory_store = ""
    job.script = ""
    job.command = ""
    job.id = "j1"
    job.name = "test-job"
    job.persistent_session = True
    job.agent_sequence = agent_sequence
    job.agent_id = None
    job.channel = ""
    job.created_by = "U1"
    job.approval_mode = "auto"
    job.env = None
    job.acked_items = []
    job.silent = False
    job.thread_ts = None
    job.last_posted_hash = ""
    job.consecutive_dupes = 0
    job.last_posted_at = 0.0
    job.last_failure_hash = ""
    job.last_failure_at = 0.0
    job.consecutive_failures = 0
    job.run_never_started = False
    return job


def _orchestrator(pending) -> MagicMock:
    """An orchestrator whose sub-agent probe answers *pending* (a value or a callable)."""
    orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
    orch.sessions = _mock_sessions()
    orch.ctx_builder = _mock_context_builder()
    orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
    orch.ctx_builder.hooks = MagicMock()
    orch.dashboard_state = _mock_dashboard_state()
    orch.slack = MagicMock()
    orch.slack.open_dm = AsyncMock(return_value="D_U1")
    orch.slack.post_blocks = AsyncMock(return_value="ts1")
    orch.slack.post_message = AsyncMock()
    orch.subagent_mgr = MagicMock()
    orch.subagent_mgr.running = []
    orch.subagent_mgr.queued_count_for_async = AsyncMock(return_value=0)
    if callable(pending):
        orch.subagent_mgr.has_pending_work_for_async = AsyncMock(side_effect=pending)
    else:
        orch.subagent_mgr.has_pending_work_for_async = AsyncMock(return_value=pending)
    return orch


async def _on_job(orch, trigger: str):
    with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
        mock_cs_inst = MagicMock()
        mock_cs_inst.start = AsyncMock()
        mock_cs_inst.start_reaper = MagicMock()
        mock_cs_inst.run_trigger = MagicMock(return_value=trigger)
        mock_cs.create = AsyncMock(return_value=mock_cs_inst)
        await orch._init_cron()
    return mock_cs.create.call_args[1]["on_job"]


def _persons_batch(key: str) -> MagicMock:
    """A ``cron:`` batch a person's turn projected with folder PreToolUse hooks."""
    from kiro_crew.acp.kas_permissions import CREW_FOLDER_PRE_TOOL_KEY
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    person = MagicMock()
    person.capabilities = capabilities_for(ACP_BACKEND_KAS)
    person._session_key = key
    person.cwd = "/checkout"
    person.kas_folder_hooks_projected = True
    person.kas_projected_agent = "proj"
    person.kas_registered_agents = [{"id": "proj", CREW_FOLDER_PRE_TOOL_KEY: True}]
    return person


def _sequence_patches(collect, reprojects=None):
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch("kiro_crew.slack.gateway.publish_turn_identity", new_callable=AsyncMock)
    )
    stack.enter_context(patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=collect))
    stack.enter_context(
        patch(
            "kiro_crew.slack.gateway.build_cron_session_context",
            return_value=("cron:j1", "run task"),
        )
    )
    if reprojects is not None:
        stack.enter_context(
            patch(
                "kiro_crew.agent_sdk.kas_project_hooks.scheduled_turn_reprojects",
                side_effect=reprojects,
            )
        )
    return stack


@pytest.mark.asyncio
async def test_a_sequence_step_on_a_persons_batch_with_sub_agents_runs():
    """Past the sweep a step is never deferred for its batch's kind: with sub-agent
    work pending on its key it runs as projected, so no completed step replays."""
    orch = _orchestrator(lambda key: key == "cron:j1:worker")
    callback = await _on_job(orch, "scheduled")
    job = _job(["planner", "worker"])

    # The planner step's run leaves the worker's session on a person's batch.
    ran: list[str] = []
    resets_at_stream: list[list[str]] = []

    async def collect(*_a, **_kw):
        ran.append("step")
        resets_at_stream.append([c.args[0] for c in orch.sessions.reset.await_args_list])
        return "cron result"

    with _sequence_patches(collect, reprojects=lambda *_a: bool(ran)):
        await callback(job)

    assert ran == ["step", "step"]
    assert job.run_never_started is False
    # The worker step ran on its batch: no kind reset under its sub-agents.
    assert "cron:j1:worker" not in resets_at_stream[-1]


@pytest.mark.asyncio
async def test_a_sequence_defers_at_its_sweep_on_a_persons_batch_with_sub_agents():
    """The sweep sees the worker's key on a person's batch with sub-agent work
    pending: no step runs, and the run is marked never started."""
    orch = _orchestrator(lambda key: key == "cron:j1:worker")
    worker = _persons_batch("cron:j1:worker")
    orch.sessions.get_provider = MagicMock(
        side_effect=lambda key: worker if key == "cron:j1:worker" else None
    )
    callback = await _on_job(orch, "scheduled")
    job = _job(["planner", "worker"])
    ran: list[str] = []

    async def collect(*_a, **_kw):
        ran.append("step")
        return "cron result"

    with _sequence_patches(collect):
        await callback(job)

    assert (ran, job.run_never_started) == ([], True)


def _single_run_patches(stream_result="cron result"):
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch("kiro_crew.slack.gateway.publish_turn_identity", new_callable=AsyncMock)
    )
    stream = stack.enter_context(
        patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value=stream_result,
        )
    )
    stack.enter_context(
        patch(
            "kiro_crew.slack.gateway.build_cron_session_context",
            return_value=("cron:j1", "run task"),
        )
    )
    stack.enter_context(
        patch(
            "kiro_crew.agent_sdk.spec_hooks.hook_projection_stale",
            new_callable=AsyncMock,
            return_value=False,
        )
    )
    return stack, stream


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["manual", "scheduled"])
async def test_a_single_run_on_a_persons_batch_with_sub_agents_never_resets(trigger):
    """A batch a person's turn projected with folder PreToolUse hooks, sub-agent work
    pending on its key: a scheduled fire defers, a manual Run runs as projected;
    neither resets the key."""
    orch = _orchestrator(True)
    orch.sessions.get_or_create = AsyncMock(return_value=(_persons_batch("cron:j1"), False, True))
    callback = await _on_job(orch, trigger)
    job = _job([])

    stack, stream = _single_run_patches()
    with stack:
        await callback(job)

    assert "cron:j1" not in [c.args[0] for c in orch.sessions.reset.await_args_list]
    assert (stream.await_count, job.run_never_started) == (
        (1, False) if trigger == "manual" else (0, True)
    )


@pytest.mark.asyncio
async def test_a_scheduled_run_defers_when_its_reprojection_does_not_land():
    """Nothing pending, so the fire re-projects the person's batch; the re-claim
    still answers a person's batch, so the fire defers and dispatches nothing."""
    orch = _orchestrator(False)
    orch.sessions.get_or_create = AsyncMock(return_value=(_persons_batch("cron:j1"), False, True))
    callback = await _on_job(orch, "scheduled")
    job = _job([])

    stack, stream = _single_run_patches()
    with stack:
        await callback(job)

    assert "cron:j1" in [c.args[0] for c in orch.sessions.reset.await_args_list]
    assert (stream.await_count, job.run_never_started) == (0, True)
