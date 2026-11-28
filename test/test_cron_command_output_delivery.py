"""A command job's clean exit delivers its stdout to the bell and its launch channel.

The run stores the output as ``last_result``; the scheduler drops the callback's
return value, so a run that only stored it delivered nothing. The output goes to
the bell and to the channel the run launched with (``channel_type``/``target_id``),
never into the origin chat, where a row would start an agent turn, and nowhere
when the job is silent. A Slack ``channel`` job gets the bell only. A failed run
is reported by the failure alert instead.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.slack.gateway as gw_mod


def _gw(monkeypatch, result):
    from cron_gateway_integration_helpers import _make_gw

    gw = _make_gw()
    gw._alert_cron_failure = AsyncMock()
    gw._deliver_script_result = AsyncMock()
    monkeypatch.setattr(gw_mod, "sel", MagicMock(name="sel"))
    monkeypatch.setattr(gw_mod, "_await_cron_fire_time_gate", AsyncMock(return_value=(None, False)))
    monkeypatch.setattr(gw_mod, "run_in_cron_pool", AsyncMock(return_value=result))
    return gw


@pytest.mark.asyncio
async def test_a_clean_exit_delivers_its_output_to_the_jobs_destination(monkeypatch):
    from cron_gateway_integration_helpers import _make_command_job

    gw = _gw(monkeypatch, {"status": "ok", "exit_code": 0, "output": "disk 41% used\n"})
    job = _make_command_job(channel_type="slack", target_id="C1")

    result = await gw._cron_run_command(job)

    assert job.last_status == "ok"
    assert result == job.last_result
    gw._deliver_script_result.assert_awaited_once()
    args, kwargs = gw._deliver_script_result.await_args
    assert args[0] is job
    assert args[1] == job.last_result
    assert "disk 41% used" in args[1]
    assert kwargs.get("remove", False) is False
    assert kwargs["pin"] == ("slack", "C1")
    assert kwargs["to_session"] is False
    gw._alert_cron_failure.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["", "  \n"])
async def test_a_clean_exit_with_no_output_delivers_nothing(monkeypatch, output):
    from cron_gateway_integration_helpers import _make_command_job

    gw = _gw(monkeypatch, {"status": "ok", "exit_code": 0, "output": output})
    job = _make_command_job()

    await gw._cron_run_command(job)

    assert job.last_status == "ok"
    gw._deliver_script_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_exit_is_alerted_not_delivered(monkeypatch):
    from cron_gateway_integration_helpers import _make_command_job

    gw = _gw(monkeypatch, {"status": "error", "exit_code": 2, "output": "boom"})
    job = _make_command_job()

    await gw._cron_run_command(job)

    assert job.last_status == "error"
    gw._alert_cron_failure.assert_awaited_once()
    gw._deliver_script_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_silent_job_delivers_nothing(monkeypatch):
    from cron_gateway_integration_helpers import _make_command_job

    gw = _gw(monkeypatch, {"status": "ok", "exit_code": 0, "output": "hello"})
    del gw._deliver_script_result  # the real delivery, which owns the silent rule
    gw._deliver_cron_to_channel = AsyncMock()
    job = _make_command_job(silent=True, session_key="dashboard:chat-1")

    await gw._cron_run_command(job)

    assert job.last_status == "ok"
    gw.dashboard_state.notify.assert_not_called()
    gw._deliver_cron_to_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_origin_chat_gets_a_bell_note_and_no_turn(monkeypatch):
    from cron_gateway_integration_helpers import _make_command_job

    gw = _gw(monkeypatch, {"status": "ok", "exit_code": 0, "output": "hello"})
    del gw._deliver_script_result  # the real delivery
    gw._deliver_cron_to_channel = AsyncMock()
    gw.dashboard_state.get_slot = MagicMock(return_value=MagicMock(window_pending=False))
    deliver = AsyncMock()
    monkeypatch.setattr("kiro_crew.dashboard.session_deliver.deliver", deliver)
    job = _make_command_job(session_key="dashboard:chat-1")

    await gw._cron_run_command(job)

    deliver.assert_not_awaited()
    [call] = gw.dashboard_state.notify.call_args_list
    assert call.args[0] == "cron"
    assert "hello" in call.args[2]
    gw._deliver_cron_to_channel.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_launch_pin_gets_the_output(monkeypatch):
    from cron_gateway_integration_helpers import _make_command_job

    gw = _gw(monkeypatch, {"status": "ok", "exit_code": 0, "output": "disk-ok"})
    del gw._deliver_script_result  # the real delivery
    gw._deliver_cron_to_channel = AsyncMock(return_value=True)
    gw.slack.post_message = AsyncMock()
    job = _make_command_job(channel_type="telegram", target_id="T1")

    await gw._cron_run_command(job)

    gw._deliver_cron_to_channel.assert_awaited_once()
    args, kwargs = gw._deliver_cron_to_channel.await_args
    assert args[0] == ""
    assert "disk-ok" in args[1]
    assert kwargs["pin"] == ("telegram", "T1")
    gw.slack.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_slack_channel_job_gets_the_bell_and_no_slack_post(monkeypatch):
    """A ``--channel`` job with no launch pin is not posted to Slack yet.

    The governed channel leg cannot carry Slack, and a direct post would skip the
    cron profile's ``channels`` scope, so the output reaches the bell only.
    """
    from cron_gateway_integration_helpers import _make_command_job

    gw = _gw(monkeypatch, {"status": "ok", "exit_code": 0, "output": "disk-ok"})
    del gw._deliver_script_result  # the real delivery
    gw._deliver_cron_to_channel = AsyncMock(return_value=False)
    gw.slack.post_message = AsyncMock(return_value="111.9")
    job = _make_command_job(channel="C1")

    await gw._cron_run_command(job)

    assert job.last_status == "ok"
    [call] = gw.dashboard_state.notify.call_args_list
    assert call.args[0] == "cron"
    assert "disk-ok" in call.args[2]
    gw.slack.post_message.assert_not_awaited()
