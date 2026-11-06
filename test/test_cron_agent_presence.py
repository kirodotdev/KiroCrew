"""A message cron whose agent was renamed or uninstalled fails with the agent's name.

Before, the fire dispatched the dead name anyway and the backend ran another
agent without its permissions, so the unattended run stopped on prompts.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.cron_service import agent_presence


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """These tests exercise the fire path, not cron write authorization."""


def _selection(resolved: bool):
    return MagicMock(return_value=(None, "", "", resolved))


def test_blank_name_is_never_missing() -> None:
    assert agent_presence.cron_agent_missing(object(), "", None) is False
    assert agent_presence.cron_agent_missing(object(), None, None) is False


def test_resolved_name_is_not_missing() -> None:
    with patch("kiro_crew.config.loader_agents._resolve_agent_selection", _selection(True)):
        assert agent_presence.cron_agent_missing(object(), "here", None) is False


def test_unresolved_name_is_missing_after_a_complete_rescan() -> None:
    refresh = MagicMock()
    with (
        patch("kiro_crew.config.loader_agents._resolve_agent_selection", _selection(False)),
        patch("kiro_crew.config.loader_snapshots.refresh_materialized_agents", refresh),
        patch(
            "kiro_crew.config.loader_snapshots.materialized_snapshot_complete", return_value=True
        ),
    ):
        assert agent_presence.cron_agent_missing(object(), "gone", None) is True
    refresh.assert_called_once_with()


def test_incomplete_rescan_cannot_prove_a_miss() -> None:
    with (
        patch("kiro_crew.config.loader_agents._resolve_agent_selection", _selection(False)),
        patch("kiro_crew.config.loader_snapshots.refresh_materialized_agents"),
        patch(
            "kiro_crew.config.loader_snapshots.materialized_snapshot_complete", return_value=False
        ),
    ):
        assert agent_presence.cron_agent_missing(object(), "gone", None) is False


def test_name_found_by_the_rescan_is_not_missing() -> None:
    selection = MagicMock(side_effect=[(None, "", "", False), (None, "", "new", True)])
    with (
        patch("kiro_crew.config.loader_agents._resolve_agent_selection", selection),
        patch("kiro_crew.config.loader_snapshots.refresh_materialized_agents"),
        patch(
            "kiro_crew.config.loader_snapshots.materialized_snapshot_complete", return_value=True
        ),
    ):
        assert agent_presence.cron_agent_missing(object(), "new", None) is False


def _fire(job: CronJob, missing: bool) -> MagicMock:
    """Run the real cron callback for *job* and return the gateway double."""
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.sessions.get_pid = MagicMock(return_value=None)
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.ctx_builder = MagicMock()
    gw.ctx_builder.conversation_log.get_metadata_status.return_value = ({}, True)
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.slack = MagicMock()
    gw.conv_log = None
    gw.dashboard_state = None
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._cron_runs_holding = set()
    gw._no_crons = False
    gw._interactive_approval = MagicMock(return_value=None)
    gw._alert_cron_failure = AsyncMock()
    captured: dict = {}

    async def fake_stream(client, msg, **kwargs):
        return "done"

    with (
        patch("kiro_crew.slack.gateway.stream_and_collect", fake_stream),
        patch("kiro_crew.slack.gateway.CronService") as cron_cls,
        patch.object(agent_presence, "cron_agent_missing", return_value=missing),
    ):

        def capture(on_job=None, **_kw):
            captured["cb"] = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            return svc

        cron_cls.create = AsyncMock(side_effect=capture)

        async def run() -> None:
            await gw._init_cron()
            await captured["cb"](job)

        asyncio.run(run())
    return gw


def _job() -> CronJob:
    return CronJob(
        id="gone1",
        name="nightly",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=300),
        agent_id="renamed-away",
    )


def test_missing_agent_fails_the_run_with_its_name() -> None:
    job = _job()
    gw = _fire(job, missing=True)
    gw.sessions.get_or_create.assert_not_called()
    assert job.last_status == "error"
    assert "Agent 'renamed-away' is no longer available" in job.last_error
    gw._alert_cron_failure.assert_awaited_once()
    assert job.consecutive_failures == 1


def test_an_agentless_job_is_never_refused() -> None:
    # It runs the floor or the configured default, not a name it stores.
    job = _job()
    job.agent_id = ""
    gw = _fire(job, missing=True)
    gw.sessions.get_or_create.assert_awaited()
    gw._alert_cron_failure.assert_not_awaited()


def test_present_agent_still_dispatches() -> None:
    gw = _fire(_job(), missing=False)
    gw.sessions.get_or_create.assert_awaited()
    gw._alert_cron_failure.assert_not_awaited()
