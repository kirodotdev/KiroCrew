"""With ``KIROCREW_DEV_MODE`` off no AWS Fargate session path is reachable.

A saved Fargate profile and a session bound to it: the picker offers nothing,
a start runs locally, and retention, archive and shutdown never build the
Fargate environments, so no registry is read and no AWS call is made. Each
half first proves the path is live with the flag on, so a removed guard fails
the flag-off assertion rather than passing on a dead path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

_FARGATE = {
    "id": "aws-dev",
    "name": "AWS development",
    "aws_profile": "sandbox",
    "region": "us-east-1",
    "cluster": "dev",
    "task_definition": "session-dev:1",
    "subnets": ["subnet-12345678"],
    "security_groups": ["sg-12345678"],
    "assign_public_ip": False,
    "workdir": "/workspace",
}
_KEY = "dashboard:fargate-chat"


@pytest.fixture
def fargate_setup(tmp_path, monkeypatch):
    from kiro_crew.coder import retention, session_launch
    from kiro_crew.config import live, paths
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.sections import SessionEnvironmentsConfig
    from kiro_crew.fargate_session.launch import FargateSessionEnvironments, SessionTaskRegistry

    monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
    raw = {"coder_url": "", "profiles": [], "fargate_profiles": [dict(_FARGATE)]}
    config = SimpleNamespace(session_environments=SessionEnvironmentsConfig.from_raw(raw))
    monkeypatch.setattr(live, "snapshot", lambda: config)
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: config)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(retention, "_section", AsyncMock(return_value=config.session_environments))
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")
    choices.set(_KEY, _FARGATE["id"], "fargate")
    fakes = SimpleNamespace(
        prepare=AsyncMock(return_value="prepared"),
        keep_alive=AsyncMock(return_value=1),
        retry_stops=AsyncMock(return_value=0),
        has_task=AsyncMock(return_value=True),
        archive_session=AsyncMock(),
        registry_all=Mock(return_value=[]),
        close_forwards=AsyncMock(),
    )
    monkeypatch.setattr(FargateSessionEnvironments, "prepare", fakes.prepare)
    monkeypatch.setattr(FargateSessionEnvironments, "keep_alive", fakes.keep_alive)
    monkeypatch.setattr(FargateSessionEnvironments, "retry_stops", fakes.retry_stops)
    monkeypatch.setattr(FargateSessionEnvironments, "has_task", fakes.has_task)
    monkeypatch.setattr(FargateSessionEnvironments, "archive_session", fakes.archive_session)
    monkeypatch.setattr(SessionTaskRegistry, "all", fakes.registry_all)
    monkeypatch.setattr("kiro_crew.fargate_session.launch.close_forwards", fakes.close_forwards)
    monkeypatch.setattr(retention, "warn_unsaved_work", AsyncMock())
    return config, fakes


async def _archive_now(retention) -> None:
    lifecycle = retention.session_lifecycle(_KEY)
    await retention._archive(((_KEY, lifecycle, lifecycle.generation),))


@pytest.mark.asyncio
async def test_with_the_developer_flag_off_no_fargate_path_is_reachable(fargate_setup, monkeypatch):
    from kiro_crew.coder import retention, session_launch
    from kiro_crew.dashboard.handlers import session_environments as handlers
    from kiro_crew.slack import gateway

    config, fakes = fargate_setup
    sweeper = AsyncMock()
    monkeypatch.setattr(retention, "run_sweeper", sweeper)

    # Flag on: every path is live, so the assertions below test the flag.
    assert [p["provider"] for p in handlers._profiles()] == ["fargate"]
    assert await session_launch.prepare_for_session(_KEY) == "prepared"
    assert await retention._fargate_environments() is not None
    assert await retention.keep_alive_once() == 1
    await retention.sweep_once(keep_alive=False)
    assert fakes.retry_stops.await_count == 1
    await _archive_now(retention)
    assert fakes.archive_session.await_count == 1
    await retention.stop_for_shutdown()
    assert fakes.registry_all.call_count == 1

    monkeypatch.delenv("KIROCREW_DEV_MODE")
    assert handlers._profiles() == []
    assert await session_launch.prepare_for_session(_KEY) is None
    assert fakes.prepare.await_count == 1
    assert await retention._fargate_environments() is None
    assert await retention.keep_alive_once() == 0
    assert fakes.keep_alive.await_count == 1
    await retention.sweep_once(keep_alive=False)
    assert fakes.retry_stops.await_count == 1
    await _archive_now(retention)
    assert fakes.has_task.await_count == 1 and fakes.archive_session.await_count == 1
    await retention.stop_for_shutdown()
    assert fakes.registry_all.call_count == 1
    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", None)
    gateway._start_coder_retention(config)
    assert gateway._CODER_RETENTION_TASK is None
    sweeper.assert_not_awaited()
