"""A ``notify_only`` cron job is a reminder: its message reaches the user, no agent runs."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from cron_gateway_integration_helpers import _make_gw_for_llm, _make_llm_job, _run_llm_callback

from kiro_crew.cron import CronService


@pytest.fixture(autouse=True)
def _isolate(_floor_monkeypatch, tmp_path):
    _floor_monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    _floor_monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)
    _floor_monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
    _floor_monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:reminder-slot")


@pytest.mark.asyncio
async def test_a_reminder_fire_starts_no_agent_turn_and_rings_the_bell():
    gw = _make_gw_for_llm()
    gw._deliver_cron_to_channel = AsyncMock(return_value=False)
    job = _make_llm_job(
        name="sim", message="Update the SIM with the latest status", notify_only=True
    )

    result, stream = await _run_llm_callback(gw, job)

    stream.assert_not_called()
    gw.sessions.get_or_create.assert_not_called()
    assert result == "Update the SIM with the latest status"
    assert job.last_status == "ok"
    title, body = gw.dashboard_state.notify.call_args.args[1:3]
    assert title == "Reminder: sim"
    assert body == "Update the SIM with the latest status"


@pytest.mark.asyncio
async def test_a_plain_message_job_still_runs_its_agent_turn():
    gw = _make_gw_for_llm()
    job = _make_llm_job(message="Run daily check")

    _, stream = await _run_llm_callback(gw, job)

    stream.assert_called()


@pytest.mark.asyncio
async def test_a_reminder_posts_to_the_jobs_slack_channel():
    from kiro_crew.slack import gateway

    gw = _make_gw_for_llm()
    gw.slack = MagicMock()
    gw.slack.post_message = AsyncMock(return_value="111.1")
    gw._deliver_cron_to_channel = AsyncMock(return_value=False)
    job = _make_llm_job(name="sim", message="comment on the SIM", notify_only=True, channel="C1")

    await gateway.GatewayOrchestrator._deliver_cron_reminder(gw, job)

    channel, text, _thread = gw.slack.post_message.call_args.args
    assert channel == "C1"
    assert "comment on the SIM" in text


def test_the_flag_round_trips_and_is_refused_with_a_command(tmp_path):
    svc = CronService()
    job = svc.add_job(name="r", message="x", every_secs=3600, notify_only=True)
    plain = svc.add_job(name="p", message="x", every_secs=3600)
    raw = {j["id"]: j for j in json.loads((tmp_path / "crons.json").read_text())["jobs"]}
    assert raw[job.id]["notify_only"] is True
    assert "notify_only" not in raw[plain.id]
    assert CronService().get_job(job.id).notify_only is True
    with pytest.raises(ValueError, match="notify_only"):
        svc.add_job(name="c", message="x", every_secs=3600, notify_only=True, command="true")


def test_cron_add_takes_the_flag():
    from kiro_crew.mcp_cron import _call_tool_inner

    result = _call_tool_inner(
        "cron_add", {"name": "remind", "message": "hi", "delay": 600, "notify_only": True}
    )

    assert "Added job" in result
    job = next(j for j in CronService().list_jobs() if j.name == "remind")
    assert job.notify_only is True


@pytest.mark.asyncio
@pytest.mark.parametrize("with_bell", [True, False], ids=["bell", "no-bell"])
async def test_a_reminder_whose_slack_send_fails_is_a_failed_run(with_bell):
    """A failed send must not be consumed as a success: the one-shot is kept."""
    from kiro_crew.slack import gateway

    gw = _make_gw_for_llm()
    if not with_bell:
        gw.dashboard_state = None
    gw.slack = MagicMock()
    gw.slack.post_message = AsyncMock(side_effect=RuntimeError("slack down"))
    gw._deliver_cron_to_channel = AsyncMock(return_value=False)
    job = _make_llm_job(name="sim", message="comment on the SIM", notify_only=True, channel="C1")

    await gateway.GatewayOrchestrator._deliver_cron_reminder(gw, job)

    assert job.last_status == "error"
    assert job.last_error == "reminder was not delivered"


@pytest.mark.asyncio
async def test_a_reminder_with_no_leg_at_all_is_a_failed_run():
    from kiro_crew.slack import gateway

    gw = _make_gw_for_llm()
    gw.dashboard_state = None
    gw._deliver_cron_to_channel = AsyncMock(return_value=False)
    job = _make_llm_job(name="sim", message="stretch", notify_only=True)

    await gateway.GatewayOrchestrator._deliver_cron_reminder(gw, job)

    assert job.last_status == "error"
